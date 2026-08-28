"""
AWS AppSync Service Emulator.

GraphQL API management service — REST/JSON protocol via /v1/apis/* paths.

Supports:
  GraphQL APIs:  CreateGraphQLApi, GetGraphQLApi, ListGraphQLApis,
                 UpdateGraphQLApi, DeleteGraphQLApi
  API Keys:      CreateApiKey, ListApiKeys, DeleteApiKey
  Data Sources:  CreateDataSource, GetDataSource, ListDataSources, DeleteDataSource
  Resolvers:     CreateResolver, GetResolver, ListResolvers, DeleteResolver
  Types:         CreateType, ListTypes, GetType
  Tags:          TagResource, UntagResource, ListTagsForResource

Wire protocol:
  REST/JSON — path-based routing under /v1/apis.
  Credential scope: appsync
"""

import base64
import copy
import json
import logging
import os
import re
import time

from ministack.core.arn import ArnParseError, parse_arn
from ministack.core.persistence import PERSIST_STATE, load_state
from ministack.core.responses import (
    AccountRegionScopedDict,
    AccountScopedDict,
    error_response_json,
    get_account_id,
    get_region,
    json_response,
    new_uuid,
    set_request_region,
)

logger = logging.getLogger("appsync")

REGION = os.environ.get("MINISTACK_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_apis = AccountRegionScopedDict()            # apiId -> api record
_api_keys = AccountRegionScopedDict()        # apiId -> {keyId -> key record}
_data_sources = AccountRegionScopedDict()    # apiId -> {name -> data source record}
_resolvers = AccountRegionScopedDict()       # apiId -> {typeName -> {fieldName -> resolver record}}
_types = AccountRegionScopedDict()           # apiId -> {typeName -> type record}
_functions = AccountRegionScopedDict()       # apiId -> {functionId -> function record}
_schemas = AccountRegionScopedDict()         # apiId -> {"definition": str, "status": str, "details": str}
_caches = AccountRegionScopedDict()           # apiId -> ApiCache record
# apiId -> {cache key -> (expires_at, value)}. Separate from _caches, which holds
# the ApiCache configuration; this is the cached data itself. Not persisted: a
# restart is a cold cache, as replacing the cache instance would be on AWS.
_cache_entries: dict = {}
_tags = AccountScopedDict()            # resource_arn -> {key: value}

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_persisted():
    if not PERSIST_STATE:
        return
    data = load_state("appsync")
    if data:
        restore_state(data)
        logger.info("Loaded persisted state for appsync")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return int(time.time())


def _api_arn(api_id):
    return f"arn:aws:appsync:{get_region()}:{get_account_id()}:apis/{api_id}"


def _api_id_from_local_arn(arn):
    try:
        spec = parse_arn(arn)
    except ArnParseError:
        return None
    if spec.service != "appsync" or spec.account_id != get_account_id() or spec.region != get_region():
        return None

    prefix = "apis/"
    if not spec.resource.startswith(prefix):
        return None
    api_id = spec.resource[len(prefix):]
    return api_id if api_id and "/" not in api_id else None


def _validate_tag_resource_arn(arn):
    api_id = _api_id_from_local_arn(arn)
    api = _apis.get(api_id) if api_id else None
    if not api or api.get("arn") != arn:
        return error_response_json("NotFoundException", f"GraphQL API {api_id or arn} not found", 404)
    return None


def _select_api_region(api_id):
    """Select the unique stored region for an unsigned GraphQL data request."""
    if api_id in _apis:
        return True

    account_id = get_account_id()
    matches = [
        region
        for (stored_account, region, stored_api_id), _api in _apis.all_items()
        if stored_account == account_id and stored_api_id == api_id
    ]
    if len(matches) != 1:
        return False
    set_request_region(matches[0])
    return True


def _has_sigv4_credentials(headers, query_params):
    """Return whether the data request carries an explicit SigV4 region."""
    query_params = query_params or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.startswith("AWS4-HMAC-SHA256") and "Credential=" in auth:
        return True

    credential = (
        query_params.get("X-Amz-Credential")
        or query_params.get("x-amz-credential")
    )
    if isinstance(credential, (list, tuple)):
        credential = credential[0] if credential else ""
    return bool(credential)


def _json(status, body):
    return json_response(body, status)


# ---------------------------------------------------------------------------
# GraphQL APIs
# ---------------------------------------------------------------------------

def _create_graphql_api(body):
    api_id = new_uuid().replace("-", "")[:26]
    name = body.get("name", "")
    auth_type = body.get("authenticationType", "API_KEY")
    additional_auth = body.get("additionalAuthenticationProviders", [])
    log_config = body.get("logConfig")
    user_pool_config = body.get("userPoolConfig")
    openid_config = body.get("openIDConnectConfig")
    xray = body.get("xrayEnabled", False)
    tags = body.get("tags", {})
    lambda_auth = body.get("lambdaAuthorizerConfig")

    arn = _api_arn(api_id)
    now = _now()

    record = {
        "apiId": api_id,
        "name": name,
        "authenticationType": auth_type,
        "arn": arn,
        "uris": {
            "GRAPHQL": f"https://{api_id}.appsync-api.{get_region()}.amazonaws.com/graphql",
            "REALTIME": f"wss://{api_id}.appsync-realtime-api.{get_region()}.amazonaws.com/graphql",
        },
        "additionalAuthenticationProviders": additional_auth,
        "xrayEnabled": xray,
        # Fields with server-side defaults. Omitting them makes a Terraform plan
        # see api_type and visibility as newly set, and both force replacement —
        # so every plan wanted to recreate the API and all of its children.
        "apiType": body.get("apiType", "GRAPHQL"),
        "visibility": body.get("visibility", "GLOBAL"),
        "introspectionConfig": body.get("introspectionConfig", "ENABLED"),
        "queryDepthLimit": body.get("queryDepthLimit", 0),
        "resolverCountLimit": body.get("resolverCountLimit", 0),
        "wafWebAclArn": body.get("wafWebAclArn"),
        "createdAt": now,
        "lastUpdatedAt": now,
    }
    if log_config:
        record["logConfig"] = log_config
    if user_pool_config:
        record["userPoolConfig"] = user_pool_config
    if openid_config:
        record["openIDConnectConfig"] = openid_config
    if lambda_auth:
        record["lambdaAuthorizerConfig"] = lambda_auth

    _apis[api_id] = record
    _api_keys[api_id] = {}
    _data_sources[api_id] = {}
    _resolvers[api_id] = {}
    _types[api_id] = {}

    if tags:
        _tags[arn] = tags

    return _json(200, {"graphqlApi": _api_with_tags(record)})


def _api_with_tags(api):
    """AWS returns tags on the GraphqlApi itself, which is where the Terraform
    provider reads them. Merged in on read rather than copied onto the record so
    TagResource and UntagResource stay reflected without a second write."""
    return {**api, "tags": dict(_tags.get(api.get("arn", ""), {}))}


def _get_graphql_api(api_id):
    api = _apis.get(api_id)
    if not api:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    return _json(200, {"graphqlApi": _api_with_tags(api)})


def _list_graphql_apis(query_params):
    apis = [_api_with_tags(a) for a in _apis.values()]
    return _json(200, {"graphqlApis": apis})


def _update_graphql_api(api_id, body):
    api = _apis.get(api_id)
    if not api:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    if "name" in body:
        api["name"] = body["name"]
    if "authenticationType" in body:
        api["authenticationType"] = body["authenticationType"]
    if "additionalAuthenticationProviders" in body:
        api["additionalAuthenticationProviders"] = body["additionalAuthenticationProviders"]
    if "logConfig" in body:
        api["logConfig"] = body["logConfig"]
    if "userPoolConfig" in body:
        api["userPoolConfig"] = body["userPoolConfig"]
    if "openIDConnectConfig" in body:
        api["openIDConnectConfig"] = body["openIDConnectConfig"]
    if "xrayEnabled" in body:
        api["xrayEnabled"] = body["xrayEnabled"]
    if "lambdaAuthorizerConfig" in body:
        api["lambdaAuthorizerConfig"] = body["lambdaAuthorizerConfig"]

    api["lastUpdatedAt"] = _now()
    return _json(200, {"graphqlApi": api})


def _delete_graphql_api(api_id):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    arn = _apis[api_id]["arn"]
    del _apis[api_id]
    _api_keys.pop(api_id, None)
    _data_sources.pop(api_id, None)
    _resolvers.pop(api_id, None)
    _types.pop(api_id, None)
    _functions.pop(api_id, None)
    _schemas.pop(api_id, None)
    _caches.pop(api_id, None)
    _cache_entries.pop(api_id, None)
    _tags.pop(arn, None)

    return _json(200, {})


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

def _create_api_key(api_id, body):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    key_id = "da2-" + new_uuid()[:26]
    now = _now()
    expires = body.get("expires", now + 604800)  # default 7 days
    description = body.get("description", "")

    record = {
        "id": key_id,
        "description": description,
        "expires": expires,
        "createdAt": now,
        "lastUpdatedAt": now,
        "deletes": expires + 5184000,  # 60 days after expiry
    }

    _api_keys.setdefault(api_id, {})[key_id] = record
    return _json(200, {"apiKey": record})


def _list_api_keys(api_id):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    keys = list(_api_keys.get(api_id, {}).values())
    return _json(200, {"apiKeys": keys})


def _delete_api_key(api_id, key_id):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    keys = _api_keys.get(api_id, {})
    if key_id not in keys:
        return error_response_json("NotFoundException", f"API key {key_id} not found", 404)

    del keys[key_id]
    return _json(200, {})


# ---------------------------------------------------------------------------
# Data Sources
# ---------------------------------------------------------------------------

def _create_data_source(api_id, body):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    name = body.get("name", "")
    ds_type = body.get("type", "NONE")
    description = body.get("description", "")
    service_role_arn = body.get("serviceRoleArn", "")

    arn = f"{_apis[api_id]['arn']}/datasources/{name}"

    record = {
        "dataSourceArn": arn,
        "name": name,
        "type": ds_type,
        "description": description,
        "serviceRoleArn": service_role_arn,
        "createdAt": _now(),
        "lastUpdatedAt": _now(),
    }

    if ds_type == "AMAZON_DYNAMODB":
        record["dynamodbConfig"] = body.get("dynamodbConfig", {})
    elif ds_type == "AWS_LAMBDA":
        record["lambdaConfig"] = body.get("lambdaConfig", {})
    elif ds_type == "AMAZON_ELASTICSEARCH" or ds_type == "AMAZON_OPENSEARCH_SERVICE":
        record["elasticsearchConfig"] = body.get("elasticsearchConfig", {})
    elif ds_type == "HTTP":
        record["httpConfig"] = body.get("httpConfig", {})
    elif ds_type == "RELATIONAL_DATABASE":
        record["relationalDatabaseConfig"] = body.get("relationalDatabaseConfig", {})

    _data_sources.setdefault(api_id, {})[name] = record
    return _json(200, {"dataSource": record})


def _get_data_source(api_id, name):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    ds = _data_sources.get(api_id, {}).get(name)
    if not ds:
        return error_response_json("NotFoundException", f"Data source {name} not found", 404)

    return _json(200, {"dataSource": ds})


def _list_data_sources(api_id):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    sources = list(_data_sources.get(api_id, {}).values())
    return _json(200, {"dataSources": sources})


def _delete_data_source(api_id, name):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    sources = _data_sources.get(api_id, {})
    if name not in sources:
        return error_response_json("NotFoundException", f"Data source {name} not found", 404)

    del sources[name]
    return _json(200, {})


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def _create_resolver(api_id, type_name, body):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    field_name = body.get("fieldName", "")
    data_source_name = body.get("dataSourceName")
    request_template = body.get("requestMappingTemplate", "")
    response_template = body.get("responseMappingTemplate", "")
    kind = body.get("kind", "UNIT")
    pipeline_config = body.get("pipelineConfig")
    caching_config = body.get("cachingConfig")
    runtime = body.get("runtime")
    code = body.get("code")

    arn = f"{_apis[api_id]['arn']}/types/{type_name}/resolvers/{field_name}"

    record = {
        "typeName": type_name,
        "fieldName": field_name,
        "dataSourceName": data_source_name,
        "resolverArn": arn,
        "requestMappingTemplate": request_template,
        "responseMappingTemplate": response_template,
        "kind": kind,
        "createdAt": _now(),
        "lastUpdatedAt": _now(),
    }
    if pipeline_config:
        record["pipelineConfig"] = pipeline_config
    if caching_config:
        record["cachingConfig"] = caching_config
    if runtime:
        record["runtime"] = runtime
    if code:
        record["code"] = code

    _resolvers.setdefault(api_id, {}).setdefault(type_name, {})[field_name] = record
    return _json(200, {"resolver": record})


def _get_resolver(api_id, type_name, field_name):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    resolver = _resolvers.get(api_id, {}).get(type_name, {}).get(field_name)
    if not resolver:
        return error_response_json("NotFoundException",
                                   f"Resolver {type_name}.{field_name} not found", 404)

    return _json(200, {"resolver": resolver})


def _list_resolvers(api_id, type_name):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    resolvers = list(_resolvers.get(api_id, {}).get(type_name, {}).values())
    return _json(200, {"resolvers": resolvers})


def _delete_resolver(api_id, type_name, field_name):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    type_resolvers = _resolvers.get(api_id, {}).get(type_name, {})
    if field_name not in type_resolvers:
        return error_response_json("NotFoundException",
                                   f"Resolver {type_name}.{field_name} not found", 404)

    del type_resolvers[field_name]
    return _json(200, {})


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

def _create_type(api_id, body):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    definition = body.get("definition", "")
    fmt = body.get("format", "SDL")

    # Extract type name from SDL definition (e.g. "type Query { ... }" -> "Query")
    name_match = re.search(r"(?:type|input|enum|interface|union|scalar)\s+(\w+)", definition)
    type_name = name_match.group(1) if name_match else "Unknown"

    arn = f"{_apis[api_id]['arn']}/types/{type_name}"

    record = {
        "name": type_name,
        "description": body.get("description", ""),
        "arn": arn,
        "definition": definition,
        "format": fmt,
        "createdAt": _now(),
        "lastUpdatedAt": _now(),
    }

    _types.setdefault(api_id, {})[type_name] = record
    return _json(200, {"type": record})


def _get_type(api_id, type_name, query_params):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    fmt = "SDL"
    if query_params.get("format"):
        fmt_val = query_params["format"]
        fmt = fmt_val[0] if isinstance(fmt_val, list) else fmt_val

    t = _types.get(api_id, {}).get(type_name)
    if not t:
        return error_response_json("NotFoundException", f"Type {type_name} not found", 404)

    return _json(200, {"type": t})


def _list_types(api_id, query_params):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    types = list(_types.get(api_id, {}).values())
    return _json(200, {"types": types})


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _start_schema_creation(api_id, body):
    """StartSchemaCreation.

    AWS accepts the SDL, validates and compiles it asynchronously, and the caller
    polls GetSchemaCreationStatus until SUCCESS or FAILED. The definition arrives
    base64-encoded because it is a blob member.

    The SDL is stored verbatim rather than parsed: nothing here consumes a type
    graph — resolvers are addressed by type and field name, and _execute_graphql
    resolves fields against the registered resolvers — so parsing would add a
    dependency and a new failure mode without changing any behaviour. Compilation
    is therefore synchronous and always succeeds, and the status is reported the
    way a completed creation reports it.
    """
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    definition = body.get("definition", "")
    if not definition:
        return error_response_json("BadRequestException", "definition is required", 400)

    if isinstance(definition, str):
        try:
            definition = base64.b64decode(definition).decode("utf-8")
        except Exception:
            # Already-plain SDL: accept it rather than refusing a readable schema.
            pass
    elif isinstance(definition, (bytes, bytearray)):
        definition = definition.decode("utf-8", "replace")

    _schemas[api_id] = {
        "definition": definition,
        "status": "SUCCESS",
        "details": "Schema creation successful.",
    }
    logger.info("AppSync: schema created for %s (%d bytes)", api_id, len(definition))
    return _json(200, {"status": "PROCESSING"})


def _get_schema_creation_status(api_id):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    schema = _schemas.get(api_id)
    if not schema:
        return _json(200, {"status": "NOT_APPLICABLE", "details": ""})
    return _json(200, {"status": schema["status"], "details": schema["details"]})


def _get_introspection_schema(api_id, query_params):
    """GetIntrospectionSchema — the response body is the schema blob itself."""
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    schema = _schemas.get(api_id)
    if not schema:
        return error_response_json("GraphQLSchemaException", "No schema found for this API", 404)

    fmt = (query_params.get("format") or ["SDL"])
    fmt = (fmt[0] if isinstance(fmt, list) else fmt or "SDL").upper()
    if fmt not in ("SDL", "JSON"):
        return error_response_json("BadRequestException", f"Unsupported format: {fmt}", 400)
    if fmt == "JSON":
        # A JSON introspection result requires a parsed type graph, which is
        # deliberately not built here; SDL is what tooling against MiniStack uses.
        return error_response_json(
            "BadRequestException",
            "JSON introspection is not supported by MiniStack; request format=SDL.", 400)

    return 200, {"Content-Type": "application/octet-stream"}, schema["definition"].encode("utf-8")


def _is_ok(response):
    """True when a handler tuple carries a 2xx status."""
    return isinstance(response, tuple) and 200 <= response[0] < 300


def _update_data_source(api_id, name, body):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    if name not in _data_sources.get(api_id, {}):
        return error_response_json("NotFoundException", f"Data source {name} not found", 404)
    created_at = _data_sources[api_id][name].get("createdAt")
    body = dict(body)
    body["name"] = name
    response = _create_data_source(api_id, body)
    if not _is_ok(response):
        return response
    # AWS keeps the original creation time across an update, so restore it on the
    # stored record and answer with that rather than the freshly stamped one.
    record = _data_sources[api_id][name]
    if created_at:
        record["createdAt"] = created_at
    return _json(200, {"dataSource": record})


def _update_resolver(api_id, type_name, field_name, body):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    if field_name not in _resolvers.get(api_id, {}).get(type_name, {}):
        return error_response_json(
            "NotFoundException", f"Resolver {type_name}.{field_name} not found", 404)
    created_at = _resolvers[api_id][type_name][field_name].get("createdAt")
    body = dict(body)
    body["fieldName"] = field_name
    response = _create_resolver(api_id, type_name, body)
    if not _is_ok(response):
        return response
    record = _resolvers[api_id][type_name][field_name]
    if created_at:
        record["createdAt"] = created_at
    return _json(200, {"resolver": record})


def _update_type(api_id, type_name, body):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    if type_name not in _types.get(api_id, {}):
        return error_response_json("NotFoundException", f"Type {type_name} not found", 404)
    body = dict(body)
    body.setdefault("name", type_name)
    return _create_type(api_id, body)


def _update_api_key(api_id, key_id, body):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    key = _api_keys.get(api_id, {}).get(key_id)
    if not key:
        return error_response_json("NotFoundException", f"API key {key_id} not found", 404)
    if body.get("description") is not None:
        key["description"] = body["description"]
    if body.get("expires") is not None:
        key["expires"] = int(body["expires"])
    return _json(200, {"apiKey": key})


def _put_environment_variables(api_id, body):
    """PutGraphqlApiEnvironmentVariables — replaces the whole map, as AWS does."""
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    env = body.get("environmentVariables")
    if env is None:
        return error_response_json("BadRequestException", "environmentVariables is required", 400)
    if not isinstance(env, dict):
        return error_response_json("BadRequestException", "environmentVariables must be a map", 400)
    _apis[api_id]["environmentVariables"] = env
    return _json(200, {"environmentVariables": env})


def _get_environment_variables(api_id):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    return _json(200, {"environmentVariables": _apis[api_id].get("environmentVariables", {})})


# ---------------------------------------------------------------------------
# Pipeline functions
# ---------------------------------------------------------------------------

def _function_record(api_id, function_id, body):
    arn = f"{_apis[api_id]['arn']}/functions/{function_id}"
    record = {
        "functionId": function_id,
        "functionArn": arn,
        "name": body.get("name", ""),
        "description": body.get("description", ""),
        "dataSourceName": body.get("dataSourceName", ""),
        "requestMappingTemplate": body.get("requestMappingTemplate", ""),
        "responseMappingTemplate": body.get("responseMappingTemplate", ""),
        "functionVersion": body.get("functionVersion", "2018-05-29"),
    }
    for optional in ("syncConfig", "maxBatchSize", "runtime", "code"):
        if body.get(optional) is not None:
            record[optional] = body[optional]
    return record


def _create_function(api_id, body):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)

    name = body.get("name")
    if not name:
        return error_response_json("BadRequestException", "name is required", 400)

    function_id = new_uuid().replace("-", "")[:26]
    record = _function_record(api_id, function_id, body)
    _functions.setdefault(api_id, {})[function_id] = record
    return _json(200, {"functionConfiguration": record})


def _get_function(api_id, function_id):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    record = _functions.get(api_id, {}).get(function_id)
    if not record:
        return error_response_json("NotFoundException", f"Function {function_id} not found", 404)
    return _json(200, {"functionConfiguration": record})


def _list_functions(api_id):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    return _json(200, {"functions": list(_functions.get(api_id, {}).values())})


def _update_function(api_id, function_id, body):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    if function_id not in _functions.get(api_id, {}):
        return error_response_json("NotFoundException", f"Function {function_id} not found", 404)
    record = _function_record(api_id, function_id, body)
    _functions[api_id][function_id] = record
    return _json(200, {"functionConfiguration": record})


def _delete_function(api_id, function_id):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    if _functions.get(api_id, {}).pop(function_id, None) is None:
        return error_response_json("NotFoundException", f"Function {function_id} not found", 404)
    return _json(200, {})


def _tag_resource(body):
    arn = body.get("resourceArn", "")
    tags = body.get("tags", {})
    validation_error = _validate_tag_resource_arn(arn)
    if validation_error:
        return validation_error
    _tags.setdefault(arn, {}).update(tags)
    return _json(200, {})


def _untag_resource(arn, query_params):
    validation_error = _validate_tag_resource_arn(arn)
    if validation_error:
        return validation_error
    tag_keys = query_params.get("tagKeys", [])
    if isinstance(tag_keys, str):
        tag_keys = [tag_keys]
    existing = _tags.get(arn, {})
    for k in tag_keys:
        existing.pop(k, None)
    return _json(200, {})


def _list_tags_for_resource(arn):
    validation_error = _validate_tag_resource_arn(arn)
    if validation_error:
        return validation_error
    tags = _tags.get(arn, {})
    return _json(200, {"tags": tags})


def _cache_record(data, existing=None):
    """Build an ApiCache from a create or update request.

    atRestEncryptionEnabled and transitEncryptionEnabled are set at create time
    and cannot be changed afterwards, so an update carries them forward from the
    existing cache rather than defaulting them back to false.
    """
    base = existing or {}
    return {
        "ttl": int(data.get("ttl", base.get("ttl", 0))),
        "apiCachingBehavior": data.get(
            "apiCachingBehavior", base.get("apiCachingBehavior", "FULL_REQUEST_CACHING")
        ),
        "type": data.get("type", base.get("type", "SMALL")),
        "transitEncryptionEnabled": bool(
            base.get("transitEncryptionEnabled",
                     data.get("transitEncryptionEnabled", False))
        ),
        "atRestEncryptionEnabled": bool(
            base.get("atRestEncryptionEnabled",
                     data.get("atRestEncryptionEnabled", False))
        ),
        # No cache is actually kept — AppSync's cache is not observable through
        # the control plane, and emulating eviction would invent behaviour a
        # caller cannot verify. The record exists so the resource can be
        # created, read back and destroyed.
        "status": "AVAILABLE",
    }


def _create_api_cache(api_id, data):
    if api_id not in _apis:
        return error_response_json("NotFoundException", f"GraphQL API {api_id} not found", 404)
    if _caches.get(api_id) is not None:
        return error_response_json(
            "BadRequestException", f"Cache already exists for API {api_id}", 400)
    record = _cache_record(data)
    _caches[api_id] = record
    return _json(200, {"apiCache": record})


def _get_api_cache(api_id):
    cache = _caches.get(api_id)
    if cache is None:
        return error_response_json(
            "NotFoundException", f"Cache not found for API {api_id}", 404)
    return _json(200, {"apiCache": cache})


def _update_api_cache(api_id, data):
    existing = _caches.get(api_id)
    if existing is None:
        return error_response_json(
            "NotFoundException", f"Cache not found for API {api_id}", 404)
    record = _cache_record(data, existing)
    _caches[api_id] = record
    return _json(200, {"apiCache": record})


def _delete_api_cache(api_id):
    if _caches.get(api_id) is None:
        return error_response_json(
            "NotFoundException", f"Cache not found for API {api_id}", 404)
    _caches.pop(api_id, None)
    _cache_entries.pop(api_id, None)
    return _json(200, {})


def _flush_api_cache(api_id):
    if _caches.get(api_id) is None:
        return error_response_json(
            "NotFoundException", f"Cache not found for API {api_id}", 404)
    _cache_entries.pop(api_id, None)
    return _json(200, {})


# ---------------------------------------------------------------------------
# Request router
# ---------------------------------------------------------------------------

# Path patterns for routing
_PATH_RE = re.compile(r"^/v1/apis(?:/([^/]+))?(?:/([^/]+))?(?:/([^/]+))?(?:/([^/]+))?(?:/([^/]+))?")
# /v1/apis                          -> groups: (None, None, None, None, None)
# /v1/apis/{apiId}                  -> groups: (apiId, None, None, None, None)
# /v1/apis/{apiId}/apikeys          -> groups: (apiId, "apikeys", None, None, None)
# /v1/apis/{apiId}/apikeys/{id}     -> groups: (apiId, "apikeys", id, None, None)
# /v1/apis/{apiId}/datasources      -> groups: (apiId, "datasources", None, None, None)
# /v1/apis/{apiId}/datasources/{n}  -> groups: (apiId, "datasources", name, None, None)
# /v1/apis/{apiId}/types            -> groups: (apiId, "types", None, None, None)
# /v1/apis/{apiId}/types/{t}/resolvers          -> (apiId, "types", t, "resolvers", None)
# /v1/apis/{apiId}/types/{t}/resolvers/{field}  -> (apiId, "types", t, "resolvers", field)


async def handle_request(method, path, headers, body, query_params):
    """Main entry point — route AppSync REST requests."""

    # AppSync Events Event APIs live under /v2/apis and share the
    # "appsync" credential scope with GraphQL, so delegate here instead of
    # teaching the central router to differentiate by credential scope.
    if path.startswith("/v2/apis") or path.startswith("/v2/tags"):
        from ministack.services import appsync_events
        return await appsync_events.handle_request(method, path, headers, body, query_params)

    # Tags endpoint: /v1/tags/{resourceArn}
    if path.startswith("/v1/tags/"):
        from urllib.parse import unquote
        arn = unquote(path[len("/v1/tags/"):])
        if method == "POST":
            data = json.loads(body) if body else {}
            data["resourceArn"] = arn
            return _tag_resource(data)
        elif method == "DELETE":
            return _untag_resource(arn, query_params)
        else:  # GET
            return _list_tags_for_resource(arn)

    # GraphQL data plane: POST /graphql or POST /v1/apis/{apiId}/graphql
    if path == "/graphql" and method == "POST":
        api_key = headers.get("x-api-key", "")
        api_id = _resolve_api_by_key(
            api_key,
            allow_cross_region=not _has_sigv4_credentials(headers, query_params),
        )
        if not api_id:
            return error_response_json("UnauthorizedException", "Valid API key required", 401)
        data = json.loads(body) if body else {}
        return _execute_graphql(api_id, data, request_headers=headers)

    if path.startswith("/v1/apis/") and path.endswith("/graphql") and method == "POST":
        parts = path.split("/")
        if len(parts) >= 5:
            api_id = parts[3]
            if not _has_sigv4_credentials(headers, query_params):
                _select_api_region(api_id)
            data = json.loads(body) if body else {}
            return _execute_graphql(api_id, data, request_headers=headers)

    m = _PATH_RE.match(path)
    if not m:
        return error_response_json("NotFoundException", f"Unknown path: {path}", 404)

    api_id, sub1, sub2, sub3, sub4 = m.groups()

    data = {}
    if body:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}

    # POST /v1/apis — CreateGraphQLApi
    if api_id is None and sub1 is None:
        if method == "POST":
            return _create_graphql_api(data)
        elif method == "GET":
            return _list_graphql_apis(query_params)

    # /v1/apis/{apiId}
    if api_id and sub1 is None:
        if method == "GET":
            return _get_graphql_api(api_id)
        elif method == "POST":
            return _update_graphql_api(api_id, data)
        elif method == "DELETE":
            return _delete_graphql_api(api_id)

    # /v1/apis/{apiId}/apikeys
    # ApiCaches: /v1/apis/{apiId}/ApiCaches[/update], and the separate
    # /FlushCache path. UpdateApiCache is a POST to .../ApiCaches/update, not a
    # PUT on the collection, so it has to be matched before the bare form.
    if sub1 == "ApiCaches":
        if sub2 == "update" and method == "POST":
            return _update_api_cache(api_id, data)
        if sub2 is None:
            if method == "POST":
                return _create_api_cache(api_id, data)
            if method == "GET":
                return _get_api_cache(api_id)
            if method == "DELETE":
                return _delete_api_cache(api_id)

    if sub1 == "FlushCache" and method == "DELETE":
        return _flush_api_cache(api_id)

    if sub1 == "apikeys":
        # Real SDKs route AppSync API key operations through the v1 path even
        # for Event APIs. If the id is not a GraphQL API but is an Event API,
        # hand the request to the Events service.
        if api_id and api_id not in _apis:
            from ministack.services import appsync_events
            if api_id in appsync_events._apis:
                if sub2 is None:
                    if method == "POST":
                        return appsync_events._create_api_key(api_id, body or b"{}")
                    elif method == "GET":
                        return appsync_events._list_api_keys(api_id, query_params)
                elif method == "DELETE":
                    return appsync_events._delete_api_key(api_id, sub2)
        if sub2 is None:
            if method == "POST":
                return _create_api_key(api_id, data)
            elif method == "GET":
                return _list_api_keys(api_id)
        else:
            # /v1/apis/{apiId}/apikeys/{keyId}
            if method == "POST":
                return _update_api_key(api_id, sub2, data)
            elif method == "DELETE":
                return _delete_api_key(api_id, sub2)

    # /v1/apis/{apiId}/environmentVariables
    if sub1 == "environmentVariables" and sub2 is None:
        if method == "PUT":
            return _put_environment_variables(api_id, data)
        elif method == "GET":
            return _get_environment_variables(api_id)

    # /v1/apis/{apiId}/schemacreation
    if sub1 == "schemacreation" and sub2 is None:
        if method == "POST":
            return _start_schema_creation(api_id, data)
        elif method == "GET":
            return _get_schema_creation_status(api_id)

    # /v1/apis/{apiId}/schema
    if sub1 == "schema" and sub2 is None and method == "GET":
        return _get_introspection_schema(api_id, query_params)

    # /v1/apis/{apiId}/functions
    if sub1 == "functions":
        if sub2 is None:
            if method == "POST":
                return _create_function(api_id, data)
            elif method == "GET":
                return _list_functions(api_id)
        else:
            # /v1/apis/{apiId}/functions/{functionId}
            if method == "GET":
                return _get_function(api_id, sub2)
            elif method == "POST":
                return _update_function(api_id, sub2, data)
            elif method == "DELETE":
                return _delete_function(api_id, sub2)

    # /v1/apis/{apiId}/datasources
    if sub1 == "datasources":
        if sub2 is None:
            if method == "POST":
                return _create_data_source(api_id, data)
            elif method == "GET":
                return _list_data_sources(api_id)
        else:
            # /v1/apis/{apiId}/datasources/{name}
            if method == "GET":
                return _get_data_source(api_id, sub2)
            elif method == "POST":
                return _update_data_source(api_id, sub2, data)
            elif method == "DELETE":
                return _delete_data_source(api_id, sub2)

    # /v1/apis/{apiId}/types
    if sub1 == "types":
        if sub2 is None:
            if method == "POST":
                return _create_type(api_id, data)
            elif method == "GET":
                return _list_types(api_id, query_params)
        elif sub3 == "resolvers":
            # /v1/apis/{apiId}/types/{typeName}/resolvers
            type_name = sub2
            if sub4 is None:
                if method == "POST":
                    return _create_resolver(api_id, type_name, data)
                elif method == "GET":
                    return _list_resolvers(api_id, type_name)
            else:
                # /v1/apis/{apiId}/types/{typeName}/resolvers/{fieldName}
                field_name = sub4
                if method == "GET":
                    return _get_resolver(api_id, type_name, field_name)
                elif method == "POST":
                    return _update_resolver(api_id, type_name, field_name, data)
                elif method == "DELETE":
                    return _delete_resolver(api_id, type_name, field_name)
        else:
            # /v1/apis/{apiId}/types/{typeName}
            if sub3 is None and method == "GET":
                return _get_type(api_id, sub2, query_params)
            if sub3 is None and method == "POST":
                return _update_type(api_id, sub2, data)

    return error_response_json("BadRequestException", f"Unsupported route: {method} {path}")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def reset():
    """Clear all in-memory state."""
    _apis.clear()
    _api_keys.clear()
    _data_sources.clear()
    _resolvers.clear()
    _types.clear()
    _functions.clear()
    _schemas.clear()
    _caches.clear()
    _cache_entries.clear()
    _tags.clear()


def get_state():
    """Return a deep copy of all state for persistence."""
    return copy.deepcopy({
        "apis": _apis,
        "api_keys": _api_keys,
        "data_sources": _data_sources,
        "resolvers": _resolvers,
        "types": _types,
        "functions": _functions,
        "schemas": _schemas,
        "caches": _caches,
        "tags": _tags,
    })


def restore_state(data):
    """Restore state from persisted data."""
    reset()
    _apis.update(data.get("apis", {}))
    api_regions = {
        (account_id, api_id): region
        for (account_id, region, api_id), _api in _apis.all_items()
    }
    for store, key in (
        (_api_keys, "api_keys"),
        (_data_sources, "data_sources"),
        (_resolvers, "resolvers"),
        (_types, "types"),
        (_functions, "functions"),
        (_schemas, "schemas"),
        (_caches, "caches"),
    ):
        _restore_api_child_store(store, data.get(key, {}), api_regions)
    _tags.update(data.get("tags", {}))


def _restore_api_child_store(store, restored, api_regions):
    """Adopt legacy API children into their parent GraphQL API's region."""
    if isinstance(restored, AccountRegionScopedDict):
        store.update(restored)
        return

    if isinstance(restored, AccountScopedDict):
        items = restored._data.items()
    else:
        account_id = get_account_id()
        items = (((account_id, api_id), value) for api_id, value in restored.items())

    for (account_id, api_id), value in items:
        region = api_regions.get(
            (account_id, api_id),
            store._region_for_legacy_value(api_id, value),
        )
        store.set_scoped(account_id, region, api_id, value)


# ---------------------------------------------------------------------------
# GraphQL Data Plane — parse and execute queries against DynamoDB
# ---------------------------------------------------------------------------

import re as _re

# Simple GraphQL parser — handles queries/mutations that Amplify generates
_GQL_OP_RE = _re.compile(
    r'(?:query|mutation|subscription)\s+(\w+)?\s*(?:\(([^)]*)\))?\s*\{(.*)\}',
    _re.DOTALL,
)
_GQL_FIELD_RE = _re.compile(r'(\w+)\s*(?:\(([^)]*)\))?\s*(?:\{([^}]*)\})?')


def _resolve_api_by_key(api_key_value, allow_cross_region=True):
    """Find the API ID that owns this API key."""
    for api_id, keys in _api_keys.items():
        for kid, key in keys.items():
            if kid == api_key_value or key.get("id") == api_key_value:
                return api_id

    if not allow_cross_region:
        # Signed requests may use the sole API in their credential region,
        # but must not discover an API stored in another region.
        if len(_apis) == 1:
            return next(iter(_apis))
        return None

    account_id = get_account_id()
    for (stored_account, region, api_id), keys in _api_keys.all_items():
        if stored_account != account_id:
            continue
        for kid, key in keys.items():
            if kid == api_key_value or key.get("id") == api_key_value:
                set_request_region(region)
                return api_id

    # Fallback: if only one API exists in this account, use its region.
    matches = [
        (region, api_id)
        for (stored_account, region, api_id), _api in _apis.all_items()
        if stored_account == account_id
    ]
    if len(matches) == 1:
        region, api_id = matches[0]
        set_request_region(region)
        return api_id
    return None


class _AuthorizerRejected(Exception):
    """Sentinel raised when the Lambda authorizer denies a request.

    AWS docs are explicit: an authorizer returning `isAuthorized:false`, an
    authorizer Lambda that's unreachable, or an authorizer that raises must all
    surface to the client as `UnauthorizedException` (HTTP 401). Callers catch
    this and emit the standard AppSync error envelope.
    """


def _invoke_lambda_authorizer(
    api_id, authorizer_config, request_headers,
    *,
    query: str = "",
    variables: dict | None = None,
    operation_name: str | None = None,
):
    """Invoke the Lambda authorizer.

    Returns the identity dict (`{}` when authorized with no `resolverContext`,
    or `{"resolverContext": {...}}` when present). Raises ``_AuthorizerRejected``
    for any failure mode AWS treats as unauthorized: missing authorizer Lambda,
    invocation error, malformed response, or ``isAuthorized:false``.

    AWS stores the authorizer Lambda under ``lambdaAuthorizerConfig.authorizerUri``
    (which ``_create_graphql_api`` persists verbatim). ministack's AppSync Events
    authorizer reads the same key.
    """
    func_arn = authorizer_config.get("authorizerUri") or authorizer_config.get("authorizer_uri")
    if not func_arn:
        # Misconfigured API (lambdaAuthorizerConfig present but no Uri). Treat
        # as authorized — this is a config error surface, not a per-request
        # rejection signal.
        return {}

    import ministack.services.lambda_svc as _lambda_svc

    func, func_config, func_name = _lambda_svc._get_func_record_for_ref(func_arn)
    if not func or not func_config:
        logger.warning("Lambda authorizer %s not found in ministack", func_arn)
        raise _AuthorizerRejected("authorizer Lambda not found")

    # AWS-verified authorizer event shape — apiId / accountId / requestId /
    # queryString / operationName / variables / requestHeaders all present per
    # the AppSync Developer Guide AWS_LAMBDA authorization section.
    authorizer_event = {
        "authorizationToken": request_headers.get("authorization", ""),
        "requestContext": {
            "apiId": api_id,
            "accountId": get_account_id(),
            "requestId": new_uuid(),
            "queryString": query,
            "operationName": operation_name or "unknown",
            "variables": variables or {},
        },
        "requestHeaders": request_headers,
    }

    try:
        exec_record = _lambda_svc._execution_record_for_config(func, func_config)
        result = _lambda_svc._execute_function_with_config_scope(exec_record, authorizer_event)
    except Exception as e:
        logger.warning("Lambda authorizer invocation failed: %s", e)
        raise _AuthorizerRejected("authorizer invocation failed") from e

    if not isinstance(result, dict) or result.get("error"):
        logger.warning("Lambda authorizer execution error")
        raise _AuthorizerRejected("authorizer execution error")

    body = result.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            raise _AuthorizerRejected("authorizer returned non-JSON body")
    if not isinstance(body, dict):
        raise _AuthorizerRejected("authorizer returned non-dict body")

    if not body.get("isAuthorized", False):
        logger.warning("Lambda authorizer rejected request")
        raise _AuthorizerRejected("isAuthorized=false")

    resolver_context = body.get("resolverContext")
    if resolver_context:
        return {"resolverContext": resolver_context}
    return {}


def _unauthorized_response():
    """AppSync's wire-shape for an authorizer rejection: HTTP 401 with a
    GraphQL `errors` envelope carrying `UnauthorizedException`."""
    return _json(401, {
        "errors": [{
            "errorType": "UnauthorizedException",
            "message": "You are not authorized to make this call.",
        }],
    })


def _execute_graphql(api_id, data, request_headers=None):
    """Execute a GraphQL query/mutation against the configured resolvers."""
    query = data.get("query", "")
    variables = data.get("variables", {})
    operation_name = data.get("operationName")

    if not query.strip():
        return _json(400, {"errors": [{"message": "Query is required"}]})

    if api_id not in _apis:
        return _json(404, {"errors": [{"message": f"API {api_id} not found"}]})

    # Parse the top-level operation
    # Strip __typename fields — Amplify adds these everywhere
    query_clean = _re.sub(r'__typename\s*', '', query)

    m = _GQL_OP_RE.search(query_clean)
    if not m:
        # Try bare field query: { getUser(id: "1") { name } }
        inner = query_clean.strip().strip("{}")
        fields = _parse_fields(inner, variables)
    else:
        op_name, op_args, body = m.groups()
        fields = _parse_fields(body, variables)

    # Determine operation type
    is_mutation = query_clean.strip().startswith("mutation")

    # Determine identity from the Lambda authorizer (AWS_LAMBDA auth mode).
    # AWS contract: a rejected authorizer must surface as UnauthorizedException
    # (HTTP 401), not as a HTTP 200 with identity=null.
    identity = None
    api = _apis.get(api_id, {})
    if api.get("lambdaAuthorizerConfig"):
        try:
            identity = _invoke_lambda_authorizer(
                api_id, api["lambdaAuthorizerConfig"], request_headers or {},
                query=query,
                variables=variables,
                operation_name=operation_name,
            )
        except _AuthorizerRejected:
            return _unauthorized_response()

    results = {}
    errors = []
    for field_name, args, sub_fields in fields:
        resolver = _find_resolver(api_id, "Mutation" if is_mutation else "Query", field_name)
        if resolver:
            # A mutation is never served from cache, and never populates it.
            cache_hit = None if is_mutation else _cache_key_for(
                api_id, resolver, field_name, args, identity, {})
            if cache_hit:
                cached = _cache_get(api_id, cache_hit[0])
                if cached is not None:
                    results[field_name] = cached
                    continue
            try:
                result = _resolve_field(
                    api_id, resolver, args, sub_fields, variables,
                    field_name=field_name,
                    identity=identity,
                    request_headers=request_headers or {},
                    source={},
                )
                results[field_name] = result
                # Only a successful result is cached; caching an error would
                # make a transient failure stick for the whole ttl.
                if cache_hit and result is not None:
                    _cache_put(api_id, cache_hit[0], cache_hit[1], result)
            except Exception as e:
                errors.append({"message": str(e), "path": [field_name]})
                results[field_name] = None
        else:
            # No resolver — return mock empty result
            results[field_name] = None

    response = {"data": results}
    if errors:
        response["errors"] = errors
    return _json(200, response)


def _parse_fields(body, variables):
    """Parse GraphQL field selections into (name, args_dict, sub_fields) tuples."""
    fields = []
    for m in _GQL_FIELD_RE.finditer(body.strip()):
        name = m.group(1)
        args_str = m.group(2) or ""
        sub = m.group(3) or ""
        args = _parse_args(args_str, variables)
        sub_fields = [s.strip() for s in sub.split() if s.strip() and s.strip() != "__typename"]
        fields.append((name, args, sub_fields))
    return fields


def _parse_args(args_str, variables):
    """Parse GraphQL arguments like (id: "1") or (id: $id) into a dict."""
    args = {}
    if not args_str.strip():
        return args
    # Match key: value pairs
    for pair in _re.finditer(r'(\w+)\s*:\s*("(?:[^"\\]|\\.)*"|\$\w+|\d+(?:\.\d+)?|true|false|null|\{[^}]*\}|\[[^\]]*\])', args_str):
        key = pair.group(1)
        val = pair.group(2)
        if val.startswith("$"):
            val = variables.get(val[1:], val)
        elif val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        elif val == "true":
            val = True
        elif val == "false":
            val = False
        elif val == "null":
            val = None
        elif val.startswith("{") and val.endswith("}"):
            val = _parse_args(val[1:-1], variables)
        elif val.startswith("[") and val.endswith("]"):
            val = val  # Keep as string for now
        elif val.replace(".", "").isdigit():
            val = float(val) if "." in val else int(val)
        args[key] = val
    return args


def _cache_key_for(api_id, resolver, field_name, args, identity, source):
    """Build a cache key for this field call, or None when it must not be cached.

    Mirrors AppSync: under PER_RESOLVER_CACHING only a resolver carrying a
    cachingConfig ttl is cached; under FULL_REQUEST_CACHING every resolver is,
    using the cache's own ttl. A caching key that cannot be resolved disables
    caching for the call rather than collapsing to a shared entry — two callers
    sharing an entry they should not is worse than not caching.
    """
    cache_cfg = _caches.get(api_id)
    if not cache_cfg:
        return None
    behavior = cache_cfg.get("apiCachingBehavior", "FULL_REQUEST_CACHING")
    resolver_cfg = (resolver or {}).get("cachingConfig") or {}

    if behavior == "PER_RESOLVER_CACHING":
        ttl = resolver_cfg.get("ttl")
        if not ttl:
            return None
    else:
        ttl = cache_cfg.get("ttl")
        if not ttl:
            return None

    ctx = {"arguments": args or {}, "args": args or {},
           "identity": identity or {}, "source": source or {}}
    parts = []
    for expr in resolver_cfg.get("cachingKeys") or []:
        path = expr.split(".")
        if path and path[0].lstrip("$") in ("context", "ctx"):
            path = path[1:]
        cur = ctx
        for seg in path:
            if not isinstance(cur, dict) or seg not in cur:
                return None
            cur = cur[seg]
        parts.append(f"{expr}={json.dumps(cur, sort_keys=True, default=str)}")
    return f"{field_name}|" + "&".join(parts), int(ttl)


def _cache_get(api_id, key):
    entry = _cache_entries.get(api_id, {}).get(key)
    if not entry:
        return None
    expires_at, value = entry
    if expires_at <= time.time():
        _cache_entries.get(api_id, {}).pop(key, None)
        return None
    return copy.deepcopy(value)


def _cache_put(api_id, key, ttl, value):
    _cache_entries.setdefault(api_id, {})[key] = (time.time() + ttl, copy.deepcopy(value))


def _find_resolver(api_id, type_name, field_name):
    """Find a resolver for Query.fieldName or Mutation.fieldName."""
    resolvers = _resolvers.get(api_id, {})
    # Try exact match
    if type_name in resolvers and field_name in resolvers[type_name]:
        return resolvers[type_name][field_name]
    # Try generic match (some setups use "Query" or "Mutation" type)
    for tn in resolvers:
        if field_name in resolvers[tn]:
            return resolvers[tn][field_name]
    return None


def _resolve_field(api_id, resolver, args, sub_fields, variables,
                   field_name=None, identity=None, request_headers=None, source=None):
    """Execute a resolver against its data source (DynamoDB or Lambda)."""
    ds_name = resolver.get("dataSourceName", "")
    data_source = _data_sources.get(api_id, {}).get(ds_name)

    if not data_source:
        # No data source — return args as mock
        return args or {}

    ds_type = data_source.get("type", "NONE")

    if ds_type == "AMAZON_DYNAMODB":
        return _resolve_dynamodb(data_source, resolver, args, sub_fields)
    elif ds_type == "AWS_LAMBDA":
        return _resolve_lambda(
            api_id=api_id,
            resolver=resolver,
            data_source=data_source,
            args=args,
            field_name=field_name or resolver.get("fieldName", ""),
            identity=identity,
            request_headers=request_headers or {},
            source=source or {},
            variables=variables or {},
        )
    else:
        return args or {}


def _resolve_dynamodb(data_source, resolver, args, sub_fields):
    """Execute a DynamoDB resolver in the data source's configured region."""
    config = data_source.get("dynamodbConfig", {})
    request_region = get_region()
    data_source_region = config.get("awsRegion") or request_region
    set_request_region(data_source_region)
    try:
        return _resolve_dynamodb_in_region(data_source, resolver, args, sub_fields)
    finally:
        set_request_region(request_region)


def _resolve_dynamodb_in_region(data_source, resolver, args, sub_fields):
    """Execute a DynamoDB resolver — auto-detect operation from field name and args."""
    import ministack.services.dynamodb as _ddb

    config = data_source.get("dynamodbConfig", {})
    table_name = config.get("tableName", "")
    if not table_name:
        return None

    table = _ddb._tables.get(table_name)
    if not table:
        return None

    field_name = resolver.get("fieldName", "")

    # Auto-detect: get* → GetItem, list* → Scan, create*/update*/put* → PutItem, delete* ��� DeleteItem
    if field_name.startswith("get") or "id" in args:
        return _ddb_get_item(table, table_name, args, sub_fields)
    elif field_name.startswith("list"):
        return _ddb_scan(table, table_name, args, sub_fields)
    elif field_name.startswith("create") or field_name.startswith("put"):
        return _ddb_put_item(table, table_name, args)
    elif field_name.startswith("update"):
        return _ddb_update_item(table, table_name, args)
    elif field_name.startswith("delete"):
        return _ddb_delete_item(table, table_name, args)
    else:
        # Default: try scan
        return _ddb_scan(table, table_name, args, sub_fields)


def _ddb_get_item(table, table_name, args, sub_fields):
    """Get a single item by primary key."""
    pk_name = table["pk_name"]
    sk_name = table.get("sk_name")

    pk_val = args.get("id") or args.get(pk_name) or next(iter(args.values()), None)
    if pk_val is None:
        return None

    items = table["items"]
    pk_bucket = items.get(str(pk_val), {})

    if sk_name:
        sk_val = args.get(sk_name, "")
        item = pk_bucket.get(str(sk_val))
    else:
        # No sort key — get the single item
        item = next(iter(pk_bucket.values()), None) if pk_bucket else None

    if not item:
        return None

    return _strip_ddb_types(item, sub_fields)


def _ddb_scan(table, table_name, args, sub_fields):
    """Scan/list items, optionally with filters and pagination."""
    items = []
    limit = args.get("limit", 100)
    next_token = args.get("nextToken")

    count = 0
    for pk in sorted(table["items"].keys()):
        for sk in sorted(table["items"][pk].keys()):
            if count >= limit:
                break
            items.append(_strip_ddb_types(table["items"][pk][sk], sub_fields))
            count += 1

    # Filter if filter arg provided
    filter_arg = args.get("filter", {})
    if filter_arg and isinstance(filter_arg, dict):
        filtered = []
        for item in items:
            match = True
            for fk, fv in filter_arg.items():
                if isinstance(fv, dict) and "eq" in fv:
                    if item.get(fk) != fv["eq"]:
                        match = False
                elif item.get(fk) != fv:
                    match = False
            if match:
                filtered.append(item)
        items = filtered

    return {"items": items, "nextToken": None}


def _ddb_put_item(table, table_name, args):
    """Create/put an item."""
    from collections import defaultdict

    import ministack.services.dynamodb as _ddb

    input_data = args.get("input", args)
    pk_name = table["pk_name"]
    sk_name = table.get("sk_name")

    # Build DynamoDB-typed item
    ddb_item = {}
    for k, v in input_data.items():
        if isinstance(v, str):
            ddb_item[k] = {"S": v}
        elif isinstance(v, (int, float)):
            ddb_item[k] = {"N": str(v)}
        elif isinstance(v, bool):
            ddb_item[k] = {"BOOL": v}
        elif isinstance(v, list):
            ddb_item[k] = {"L": [{"S": str(i)} for i in v]}
        elif v is None:
            ddb_item[k] = {"NULL": True}
        else:
            ddb_item[k] = {"S": str(v)}

    # Auto-generate ID if not provided
    if pk_name not in ddb_item and "id" not in ddb_item:
        ddb_item["id" if pk_name == "id" else pk_name] = {"S": new_uuid()}

    pk_val = _ddb._extract_key_val(ddb_item.get(pk_name, {}))
    sk_val = _ddb._extract_key_val(ddb_item.get(sk_name, {})) if sk_name else ""

    if not isinstance(table["items"], defaultdict):
        table["items"] = defaultdict(dict, table["items"])

    table["items"][pk_val][sk_val] = ddb_item
    table["ItemCount"] = sum(len(v) for v in table["items"].values())

    return _strip_ddb_types(ddb_item, [])


def _ddb_update_item(table, table_name, args):
    """Update an existing item — merge input fields."""
    input_data = args.get("input", args)
    pk_name = table["pk_name"]
    pk_val = str(input_data.get("id") or input_data.get(pk_name, ""))

    if pk_val in table["items"]:
        sk = next(iter(table["items"][pk_val]), "")
        existing = table["items"][pk_val].get(sk, {})
        for k, v in input_data.items():
            if isinstance(v, str):
                existing[k] = {"S": v}
            elif isinstance(v, (int, float)):
                existing[k] = {"N": str(v)}
            elif isinstance(v, bool):
                existing[k] = {"BOOL": v}
        return _strip_ddb_types(existing, [])
    return None


def _ddb_delete_item(table, table_name, args):
    """Delete an item and return it."""
    input_data = args.get("input", args)
    pk_name = table["pk_name"]
    pk_val = str(input_data.get("id") or input_data.get(pk_name, ""))

    if pk_val in table["items"]:
        sk = next(iter(table["items"][pk_val]), "")
        item = table["items"][pk_val].pop(sk, None)
        if not table["items"][pk_val]:
            table["items"].pop(pk_val, None)
        if item:
            return _strip_ddb_types(item, [])
    return None


def _strip_ddb_types(item, sub_fields):
    """Convert DynamoDB typed attributes to plain values for GraphQL response."""
    if not item:
        return None
    result = {}
    for k, v in item.items():
        if isinstance(v, dict):
            if "S" in v:
                result[k] = v["S"]
            elif "N" in v:
                val = v["N"]
                result[k] = int(val) if "." not in val else float(val)
            elif "BOOL" in v:
                result[k] = v["BOOL"]
            elif "NULL" in v:
                result[k] = None
            elif "L" in v:
                result[k] = [_strip_ddb_types(i, []) if isinstance(i, dict) and not any(t in i for t in ("S", "N", "BOOL")) else (i.get("S") or i.get("N") or i.get("BOOL")) for i in v["L"]]
            elif "M" in v:
                result[k] = _strip_ddb_types(v["M"], [])
            else:
                result[k] = v
        else:
            result[k] = v
    if sub_fields:
        result = {k: v for k, v in result.items() if k in sub_fields or k == "id" or k == "__typename"}
    return result


def _resolve_lambda(api_id, resolver, data_source, args,
                    field_name, identity, request_headers, source, variables=None):
    """Execute a Lambda resolver by building the standard AWS AppSync resolver event."""
    config = data_source.get("lambdaConfig", {})
    func_arn = config.get("lambdaFunctionArn", "")
    if not func_arn:
        logger.warning("No lambdaFunctionArn in data source")
        return args or {}

    import ministack.services.lambda_svc as _lambda_svc
    func, func_config, func_name = _lambda_svc._get_func_record_for_ref(func_arn)
    if not func or not func_config:
        logger.warning("Lambda function %s not found", func_arn)
        return args or {}

    # AWS-standard AppSync resolver event — fieldName lives only under info.
    event = {
        "arguments": args,
        "source": source,
        "request": {"headers": request_headers},
        "prev": None,
        "stash": {},
        "info": {
            "fieldName": field_name,
            "parentTypeName": resolver.get("typeName", "Query"),
            "variables": variables or {},
        },
    }
    if identity is not None:
        # Generic pass-through of the authorizer's resolverContext. Omitted for
        # API_KEY auth; a consumer detects API-key auth via request.headers.
        event["identity"] = identity

    try:
        exec_record = _lambda_svc._execution_record_for_config(func, func_config)
        result = _lambda_svc._execute_function_with_config_scope(exec_record, event)
    except Exception as e:
        logger.error("Lambda %s invocation failed: %s", func_name, e)
        return {"errors": [f"Lambda invocation error: {str(e)}"]}

    # RIE catches an unhandled Lambda exception and returns a normal dict with
    # error=True (no Python exception bubbles up), so the try/except above does
    # not cover it. Surface execution errors as GraphQL errors, not as data.
    if not isinstance(result, dict) or result.get("error"):
        err_body = result.get("body") if isinstance(result, dict) else None
        msg = err_body.get("errorMessage") if isinstance(err_body, dict) else "Lambda execution error"
        logger.error("Lambda %s returned error: %s", func_name, msg)
        return {"errors": [msg or "Lambda execution error"]}

    body = result.get("body")
    if body is None:
        return None
    if isinstance(body, dict):
        return body
    if isinstance(body, (str, bytes)):
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            logger.error("Invalid JSON from Lambda %s", func_name)
            return {"errors": ["Invalid response format"]}
    return {"errors": ["Unexpected response type"]}

# Load persisted state (must be after restore_state is defined)
_load_persisted()
