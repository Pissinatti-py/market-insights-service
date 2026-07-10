import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter(prefix="/devtools", tags=["Dev Tools"])

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@router.get("/collection/")
async def download_openapi_collection(request: Request) -> Response:
    """
    Download the OpenAPI schema as a JSON file, with a filename based on the API title and version.

    :param request: The incoming HTTP request, used to access the OpenAPI schema and metadata.
    :type request: Request
    :return: A Response object containing the OpenAPI schema as a downloadable JSON file.
    :rtype: Response
    """
    schema = request.app.openapi()
    info = schema.get("info", {})
    title = info.get("title", "api").lower().replace(" ", "-")
    version = info.get("version", "")
    filename = f"{title}-{version}.json" if version else f"{title}.json"
    body = json.dumps(schema, ensure_ascii=False, indent=2)

    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/insomnia/")
async def download_insomnia_collection(request: Request) -> Response:
    """
    Download an Insomnia v4 export with one request_group (folder) per OpenAPI tag.

    Insomnia's built-in OpenAPI importer flattens everything into a single group.
    This endpoint generates the native ``insomnia_export_4`` format so each tag
    becomes its own folder, with one request per ``operationId``.

    :param request: The incoming HTTP request, used to access the OpenAPI schema.
    :type request: Request
    :return: A Response object containing the Insomnia collection as a downloadable JSON file.
    :rtype: Response
    """
    schema = request.app.openapi()
    info = schema.get("info", {})
    title = info.get("title", "api")
    version = info.get("version", "")
    slug = title.lower().replace(" ", "-")
    filename = f"{slug}-insomnia-{version}.json" if version else f"{slug}-insomnia.json"

    base_url = str(request.base_url).rstrip("/")
    resources = _build_insomnia_resources(schema, title, base_url)

    export = {
        "_type": "export",
        "__export_format": 4,
        "__export_date": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "__export_source": "market-insights-service.devtools",
        "resources": resources,
    }
    body = json.dumps(export, ensure_ascii=False, indent=2)

    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _stable_id(prefix: str, *parts: str) -> str:
    """
    Build a deterministic Insomnia resource id from a prefix and seed parts.

    :param prefix: Resource id prefix (e.g. ``"wrk"``, ``"env"``).
    :type prefix: str
    :param parts: Seed strings hashed into a stable UUID5.
    :type parts: str
    :return: A stable ``{prefix}_{hex}`` identifier.
    :rtype: str
    """
    seed = "|".join(parts)
    return f"{prefix}_{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex}"


def _build_insomnia_resources(schema: dict, title: str, base_url: str) -> list[dict[str, Any]]:
    """
    Build the full list of Insomnia resources (workspace, env, requests) from an OpenAPI schema.

    :param schema: The OpenAPI schema to convert.
    :type schema: dict
    :param title: Workspace title (also seeds stable resource ids).
    :type title: str
    :param base_url: Base URL stored in the Insomnia environment.
    :type base_url: str
    :return: The Insomnia export resource list.
    :rtype: list[dict[str, Any]]
    """
    workspace_id = _stable_id("wrk", title)
    env_id = _stable_id("env", title, "base")
    now_ms = int(time.time() * 1000)

    # This service is unauthenticated; the environment only needs the base URL.
    env_data: dict[str, Any] = {"base_url": base_url}

    resources: list[dict[str, Any]] = [
        {
            "_id": workspace_id,
            "_type": "workspace",
            "parentId": None,
            "name": title,
            "description": schema.get("info", {}).get("description", ""),
            "scope": "collection",
        },
        {
            "_id": env_id,
            "_type": "environment",
            "parentId": workspace_id,
            "name": "Base Environment",
            "data": env_data,
            "dataPropertyOrder": {"&": list(env_data.keys())},
            "color": None,
            "isPrivate": False,
            "metaSortKey": now_ms,
        },
    ]

    paths = schema.get("paths", {}) or {}
    components = schema.get("components", {}) or {}

    # Only create folders for tags that will actually receive a request.
    # Each operation is assigned to its LAST tag — FastAPI appends per-endpoint
    # tags after the router-level tag, so the last entry is the more specific one.
    active_tags: set[str] = set()
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            active_tags.add((op.get("tags") or ["default"])[-1])

    folder_ids: dict[str, str] = {}
    for index, tag in enumerate(sorted(active_tags, key=str.lower)):
        folder_ids[tag] = _stable_id("fld", title, tag)
        resources.append(
            {
                "_id": folder_ids[tag],
                "_type": "request_group",
                "parentId": workspace_id,
                "name": tag,
                # metaSortKey controls folder order inside the workspace. Using a
                # padded index keeps lexical sort and numeric sort aligned.
                "metaSortKey": -1_000_000 + index,
            }
        )

    # Collect requests first so they can be emitted sorted by (path, method)
    # within each folder, giving a stable A→Z ordering on import.
    requests_by_folder: dict[str, list[dict[str, Any]]] = {tag: [] for tag in folder_ids}

    for path, path_item in sorted(paths.items()):
        if not isinstance(path_item, dict):
            continue
        for method, op in sorted(path_item.items()):
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue

            tag = (op.get("tags") or ["default"])[-1]
            req_id = _stable_id("req", title, method, path)
            name = op.get("summary") or op.get("operationId") or f"{method.upper()} {path}"

            insomnia_url = "{{ _.base_url }}" + path
            parameters = _collect_parameters(op, path_item)
            query_params = [
                {"name": p["name"], "value": str(p.get("example", "")), "disabled": not p.get("required", False)}
                for p in parameters
                if p.get("in") == "query"
            ]
            headers = [
                {"name": p["name"], "value": str(p.get("example", "")), "disabled": not p.get("required", False)}
                for p in parameters
                if p.get("in") == "header"
            ]

            body = _build_request_body(op.get("requestBody"), components, headers)

            requests_by_folder.setdefault(tag, []).append(
                {
                    "_id": req_id,
                    "_type": "request",
                    "parentId": folder_ids[tag],
                    "name": name,
                    "description": op.get("description", ""),
                    "method": method.upper(),
                    "url": insomnia_url,
                    "parameters": query_params,
                    "headers": headers,
                    "body": body,
                    "authentication": {},
                    # metaSortKey is assigned below once requests are sorted.
                }
            )

    for tag in sorted(requests_by_folder, key=str.lower):
        folder_requests = sorted(requests_by_folder[tag], key=lambda r: (r["name"].lower(), r["method"]))
        for index, req in enumerate(folder_requests):
            req["metaSortKey"] = -1_000_000 + index
            resources.append(req)

    return resources


def _collect_parameters(op: dict, path_item: dict) -> list[dict]:
    """
    Merge path-level and operation-level OpenAPI parameters.

    :param op: The operation object.
    :type op: dict
    :param path_item: The path item object that contains the operation.
    :type path_item: dict
    :return: The combined list of parameter objects.
    :rtype: list[dict]
    """
    params: list[dict] = []
    for src in (path_item.get("parameters") or [], op.get("parameters") or []):
        for p in src:
            if isinstance(p, dict):
                params.append(p)
    return params


def _build_request_body(request_body: Any, components: dict, headers: list[dict]) -> dict:
    """
    Build an Insomnia request body from an OpenAPI ``requestBody``.

    Handles JSON, multipart, and url-encoded content; appends a ``Content-Type``
    header for JSON bodies.

    :param request_body: The OpenAPI ``requestBody`` object.
    :type request_body: Any
    :param components: The schema ``components`` for ``$ref`` resolution.
    :type components: dict
    :param headers: The request's header list (mutated to add ``Content-Type``).
    :type headers: list[dict]
    :return: The Insomnia body object (empty dict if there is no content).
    :rtype: dict
    """
    if not isinstance(request_body, dict):
        return {}
    content = request_body.get("content") or {}
    if not content:
        return {}

    if "application/json" in content:
        schema = content["application/json"].get("schema") or {}
        example = _example_from_schema(schema, components)
        if not any(h["name"].lower() == "content-type" for h in headers):
            headers.append({"name": "Content-Type", "value": "application/json"})
        return {
            "mimeType": "application/json",
            "text": json.dumps(example, ensure_ascii=False, indent=2),
        }

    if "multipart/form-data" in content:
        schema = content["multipart/form-data"].get("schema") or {}
        resolved = _resolve_ref(schema, components)
        props = (resolved or {}).get("properties", {}) or {}
        params = [
            {"name": k, "value": "", "type": "file" if v.get("format") == "binary" else "text"}
            for k, v in props.items()
        ]
        return {"mimeType": "multipart/form-data", "params": params}

    if "application/x-www-form-urlencoded" in content:
        schema = content["application/x-www-form-urlencoded"].get("schema") or {}
        resolved = _resolve_ref(schema, components)
        props = (resolved or {}).get("properties", {}) or {}
        params = [{"name": k, "value": ""} for k in props.keys()]
        return {"mimeType": "application/x-www-form-urlencoded", "params": params}

    # Fallback to first available content type as raw text.
    mime, _ = next(iter(content.items()))
    return {"mimeType": mime, "text": ""}


def _resolve_ref(schema: dict, components: dict) -> dict:
    """
    Resolve a local ``$ref`` against the schema components, if present.

    :param schema: A schema object, possibly a ``$ref``.
    :type schema: dict
    :param components: The OpenAPI ``components`` object.
    :type components: dict
    :return: The dereferenced schema, or the input unchanged when not a local ref.
    :rtype: dict
    """
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if not ref or not ref.startswith("#/components/"):
        return schema
    parts = ref.lstrip("#/").split("/")
    node: Any = {"components": components}
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _example_from_schema(schema: dict, components: dict, depth: int = 0) -> Any:
    """
    Synthesize an example value from an OpenAPI schema.

    Prefers an explicit ``example``/``default``/``enum``, otherwise recurses
    into composed and object/array schemas. Bounded by ``depth`` to guard
    against recursive ``$ref`` cycles.

    :param schema: The schema to derive an example from.
    :type schema: dict
    :param components: The schema ``components`` for ``$ref`` resolution.
    :type components: dict
    :param depth: Current recursion depth (internal guard), defaults to 0.
    :type depth: int
    :return: A representative example value, or None.
    :rtype: Any
    """
    if depth > 6 or not isinstance(schema, dict):
        return None

    schema = _resolve_ref(schema, components)
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]

    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema and schema[key]:
            merged: dict = {}
            for sub in schema[key]:
                merged.update(_resolve_ref(sub, components))
            return _example_from_schema(merged, components, depth + 1)

    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        return {
            name: _example_from_schema(sub, components, depth + 1)
            for name, sub in (schema.get("properties") or {}).items()
        }
    if schema_type == "array":
        return [_example_from_schema(schema.get("items") or {}, components, depth + 1)]
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0.0
    if schema_type == "boolean":
        return False
    if schema_type == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "1970-01-01T00:00:00Z"
        if fmt == "date":
            return "1970-01-01"
        if fmt == "uuid":
            return "00000000-0000-0000-0000-000000000000"
        if fmt == "email":
            return "user@example.com"
        return ""

    return None
