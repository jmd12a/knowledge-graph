from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import httpx
import typer
import yaml
from pydantic import ValidationError

from ...domain import BusinessModel
from ...domain.errors import KGError
from ...settings import Settings

app = typer.Typer(no_args_is_help=True, help="Configure, build and query source-backed knowledge graphs.")
model_app = typer.Typer(no_args_is_help=True)
kb_app = typer.Typer(no_args_is_help=True)
document_app = typer.Typer(no_args_is_help=True)
job_app = typer.Typer(no_args_is_help=True)
app.add_typer(model_app, name="model")
app.add_typer(kb_app, name="kb")
app.add_typer(document_app, name="documents")
app.add_typer(job_app, name="jobs")


@app.callback()
def configure(ctx: typer.Context, api_url: str = typer.Option("http://127.0.0.1:8000", envvar="KG_API_URL")):
    ctx.ensure_object(dict)
    ctx.obj["api_url"] = api_url.rstrip("/") + "/api/v1"


def output(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def load_config(path: Path):
    try:
        value = yaml.safe_load(path.read_text("utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise typer.BadParameter("Could not read a YAML/JSON configuration file") from exc
    if not isinstance(value, dict):
        raise typer.BadParameter("Configuration must contain an object")
    return value


def segment(value):
    return quote(value, safe="")


def request(ctx, method, path, *, body=None, key=None, params=None, files=None, data=None):
    """调用管理或检索 API，输出统一 JSON 并以非零退出码报告失败。"""
    headers = {"Idempotency-Key": key} if key is not None else {}
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.request(
                method,
                ctx.obj["api_url"] + path,
                json=body,
                headers=headers,
                params=params,
                files=files,
                data=data,
            )
    except httpx.RequestError as exc:
        output(
            {
                "code": "HTTP_UNAVAILABLE",
                "message": "Could not reach the knowledge graph API",
                "retryable": True,
            }
        )
        raise typer.Exit(1) from exc
    try:
        value = response.json()
    except ValueError:
        value = {
            "code": "INVALID_HTTP_RESPONSE",
            "message": "API did not return JSON",
            "status": response.status_code,
        }
    output(value)
    if response.is_error:
        raise typer.Exit(1)
    return value


@app.command("init-storage")
def init_storage(config: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None):
    """Initialize storage offline, with the API and Worker stopped."""
    from ...bootstrap import Context

    async def initialize():
        context = Context(Settings.load(str(config) if config else None))
        try:
            await context.initialize()
        finally:
            await context.close()

    try:
        asyncio.run(initialize())
    except KGError as exc:
        output(exc.as_dict())
        raise typer.Exit(1) from exc
    output({"status": "initialized"})


@app.command()
def serve(config: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None):
    """Run one ASGI process exposing HTTP and the five shared MCP tools."""
    import uvicorn

    settings = Settings.load(str(config) if config else None)
    if config:
        os.environ["KG_CONFIG"] = str(config.resolve())
    uvicorn.run(
        "knowledge_graph.server:create_app", factory=True, host=settings.host, port=settings.port, workers=1
    )


@app.command()
def worker(
    config: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    once: bool = False,
    role: Annotated[str | None, typer.Option(help="Distributed Worker role: prepare or publish.")] = None,
):
    """Run the local Worker or a distributed preparation or publication role."""
    from ...worker import run_worker

    try:
        asyncio.run(run_worker(Settings.load(str(config) if config else None), once=once, role=role))
    except KGError as exc:
        output(exc.as_dict())
        raise typer.Exit(1) from exc


@model_app.command("validate")
def validate_model(file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]):
    """Validate a model locally without changing current knowledge or calling a model."""
    try:
        model = BusinessModel.model_validate(load_config(file))
    except (ValidationError, KGError) as exc:
        output({"code": "INVALID_MODEL", "message": "Model configuration is invalid", "retryable": False})
        raise typer.Exit(1) from exc
    output(
        {
            "valid": True,
            "name": model.name,
            "node_types": list(model.node_types),
            "relation_types": list(model.relation_types),
        }
    )


@model_app.command("apply")
def apply_model(
    ctx: typer.Context,
    kb_id: str,
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    key: Annotated[str, typer.Option("--key", help="Explicit request idempotency key")],
):
    """Submit a model rebuild; it does not immediately replace the effective model."""
    request(ctx, "POST", "/kbs/" + segment(kb_id) + "/model", body={"model": load_config(file)}, key=key)


@kb_app.command("create")
def create_kb(
    ctx: typer.Context,
    name: str,
    model: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    embedding_profile: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
):
    body = {"name": name, "model": load_config(model)}
    if embedding_profile:
        body["embedding_profile"] = load_config(embedding_profile)
    request(ctx, "POST", "/kbs", body=body)


@kb_app.command("list")
def list_kbs(ctx: typer.Context, limit: int = 100, offset: int = 0):
    request(ctx, "GET", "/kbs", params={"limit": limit, "offset": offset})


@document_app.command("upload")
def upload_document(
    ctx: typer.Context,
    kb_id: str,
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    key: Annotated[str, typer.Option("--key")],
    source: str = "{}",
    mapping_id: str | None = None,
):
    try:
        source_data = json.loads(source)
    except ValueError as exc:
        raise typer.BadParameter("--source must be a JSON object") from exc
    if not isinstance(source_data, dict):
        raise typer.BadParameter("--source must be a JSON object")
    with file.open("rb") as stream:
        request(
            ctx,
            "POST",
            "/kbs/" + segment(kb_id) + "/documents",
            key=key,
            files={"file": (file.name, stream)},
            data={"source": source, **({"mapping_id": mapping_id} if mapping_id else {})},
        )


@document_app.command("list")
def list_documents(
    ctx: typer.Context, kb_id: str, active: bool | None = None, limit: int = 100, offset: int = 0
):
    params = {"limit": limit, "offset": offset}
    if active is not None:
        params["active"] = str(active).lower()
    request(ctx, "GET", "/kbs/" + segment(kb_id) + "/documents", params=params)


@document_app.command("get")
def get_document(ctx: typer.Context, kb_id: str, document_id: str):
    request(ctx, "GET", "/kbs/" + segment(kb_id) + "/documents/" + segment(document_id))


@document_app.command("content")
def download_document(
    ctx: typer.Context, kb_id: str, document_id: str, destination: Annotated[Path, typer.Option("--output")]
):
    url = ctx.obj["api_url"] + "/kbs/" + segment(kb_id) + "/documents/" + segment(document_id) + "/content"
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            with client.stream("GET", url) as response:
                if response.is_error:
                    output(response.read().decode("utf-8", errors="replace"))
                    raise typer.Exit(1)
                with destination.open("wb") as stream:
                    size = 0
                    for block in response.iter_bytes():
                        stream.write(block)
                        size += len(block)
    except (OSError, httpx.RequestError) as exc:
        output(
            {"code": "DOWNLOAD_FAILED", "message": "Document download did not complete", "retryable": True}
        )
        raise typer.Exit(1) from exc
    output({"path": str(destination), "byte_length": size})


@document_app.command("delete")
def delete_document(
    ctx: typer.Context, kb_id: str, document_id: str, key: Annotated[str, typer.Option("--key")]
):
    request(ctx, "DELETE", "/kbs/" + segment(kb_id) + "/documents/" + segment(document_id), key=key)


@job_app.command("get")
def get_job(ctx: typer.Context, job_id: str):
    request(ctx, "GET", "/jobs/" + segment(job_id))


@job_app.command("issues")
def job_issues(ctx: typer.Context, job_id: str, limit: int = 100, offset: int = 0):
    request(ctx, "GET", "/jobs/" + segment(job_id) + "/issues", params={"limit": limit, "offset": offset})


@job_app.command("retry")
def retry_job(ctx: typer.Context, job_id: str, generation: Annotated[int, typer.Option("--generation", min=0)]):
    request(ctx, "POST", "/jobs/" + segment(job_id) + "/retry", params={"generation": generation})


@app.command()
def reindex(
    ctx: typer.Context,
    kb_id: str,
    profile: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    key: Annotated[str, typer.Option("--key")],
):
    request(
        ctx,
        "POST",
        "/kbs/" + segment(kb_id) + "/reindex",
        body={"embedding_profile": load_config(profile)},
        key=key,
    )


@app.command("describe-model")
def describe_model(ctx: typer.Context, kb_id: str):
    request(ctx, "GET", "/kbs/" + segment(kb_id) + "/model")


@app.command()
def search(
    ctx: typer.Context, kb_id: str, query: str, chunks: bool = False, semantic: bool = True, limit: int = 20
):
    request(
        ctx,
        "POST",
        "/kbs/" + segment(kb_id) + "/knowledge/search",
        body={
            "query": query,
            "kinds": ["entity", "chunk"] if chunks else ["entity"],
            "semantic": semantic,
            "limit": limit,
        },
    )


@app.command("get")
def get_knowledge(ctx: typer.Context, kb_id: str, kind: str, object_id: str, evidence_limit: int = 100):
    request(
        ctx,
        "GET",
        "/kbs/" + segment(kb_id) + "/knowledge/" + segment(kind) + "/" + segment(object_id),
        params={"evidence_limit": evidence_limit},
    )


@app.command("query-graph")
def query_graph(
    ctx: typer.Context, kb_id: str, file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]
):
    request(ctx, "POST", "/kbs/" + segment(kb_id) + "/graph/query", body=load_config(file))


@app.command()
def retrieve(
    ctx: typer.Context,
    kb_id: str,
    query: str,
    file: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
):
    body = load_config(file) if file else {}
    body["query"] = query
    request(ctx, "POST", "/kbs/" + segment(kb_id) + "/retrieve", body=body)


if __name__ == "__main__":
    app()
