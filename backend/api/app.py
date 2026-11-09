"""FastAPI uçları. İş mantığı backend.services içindedir; burada yalnız HTTP eşlemesi vardır."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from backend import services
from backend.integrations import integration_status
from backend.schemas import Layout, OptimizationConfig, OptimizationResult, Project, SimConfig, SimResult

router = APIRouter()


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(services.NotFoundError)
    async def _nf(_: Request, exc: services.NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(services.ModuleNotReady)
    async def _nr(_: Request, exc: services.ModuleNotReady) -> JSONResponse:
        return JSONResponse(status_code=501, content={"detail": str(exc)})

    @app.exception_handler(services.ImportDataError)
    async def _bad(_: Request, exc: services.ImportDataError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/integrations")
def integrations() -> dict[str, Any]:
    return integration_status()


# ---- projeler
@router.get("/projects")
def list_projects() -> list[dict[str, Any]]:
    return services.list_projects()


@router.post("/projects", response_model=Project, status_code=201)
def create_project(project: Project) -> Project:
    return services.save_project(project)


@router.post("/projects/demo", response_model=Project)
def demo_project() -> Project:
    return services.ensure_demo_project()


@router.post("/projects/import/json", response_model=Project, status_code=201)
async def import_json(request: Request) -> Project:
    return services.import_project_json((await request.body()).decode("utf-8"))


@router.post("/projects/import/excel", response_model=Project, status_code=201)
async def import_excel(request: Request) -> Project:
    return services.import_project_excel(await request.body())


@router.get("/projects/{project_id}", response_model=Project)
def get_project(project_id: str) -> Project:
    return services.get_project(project_id)


@router.put("/projects/{project_id}", response_model=Project)
def put_project(project_id: str, project: Project) -> Project:
    if project.id != project_id:
        raise HTTPException(422, "Gövdedeki proje id'si URL ile uyuşmuyor.")
    return services.save_project(project)


@router.get("/projects/{project_id}/export/json")
def export_json(project_id: str) -> Response:
    return Response(services.export_project_json(project_id), media_type="application/json")


@router.get("/projects/{project_id}/export/excel")
def export_excel(project_id: str) -> Response:
    return Response(
        services.export_project_excel(project_id),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{project_id}.xlsx"'},
    )


@router.get("/projects/{project_id}/export/workstations.csv")
def export_ws_csv(project_id: str) -> Response:
    return Response(services.export_workstations_csv(services.get_layout(project_id)), media_type="text/csv")


# ---- layout
@router.get("/projects/{project_id}/layout", response_model=Layout)
def get_layout(project_id: str) -> Layout:
    return services.get_layout(project_id)


@router.put("/projects/{project_id}/layout", response_model=Layout)
def put_layout(project_id: str, layout: Layout) -> Layout:
    """Yeni layout sürümü olarak kaydeder (eski sürümler korunur) ve aktif yapar."""
    return services.save_layout_version(project_id, layout)


@router.post("/projects/{project_id}/layout/validate")
def validate_layout(project_id: str, layout_id: str | None = None) -> Any:
    return services.validate_layout_for(project_id, layout_id)


@router.get("/projects/{project_id}/layout/versions")
def layout_versions(project_id: str) -> list[dict[str, Any]]:
    return services.list_layout_versions(project_id)


@router.get("/projects/{project_id}/layout/versions/{layout_id}", response_model=Layout)
def layout_version(project_id: str, layout_id: str) -> Layout:
    return services.get_layout(project_id, layout_id)


@router.post("/projects/{project_id}/layout/versions/{layout_id}/activate", response_model=Layout)
def activate_layout(project_id: str, layout_id: str) -> Layout:
    return services.activate_layout_version(project_id, layout_id)


# ---- simülasyon
@router.post("/projects/{project_id}/simulations", status_code=201)
def run_simulation(project_id: str, config: SimConfig | None = None, layout_id: str | None = None,
                   scenario_name: str | None = None) -> dict[str, Any]:
    run_id, result = services.run_simulation_for(project_id, config, layout_id, scenario_name)
    return {"run_id": run_id, "result": result.model_dump(mode="json")}


@router.get("/projects/{project_id}/simulations")
def list_simulations(project_id: str) -> list[dict[str, Any]]:
    return services.list_simulation_runs(project_id)


@router.get("/projects/{project_id}/simulations/latest", response_model=SimResult)
def latest_simulation(project_id: str, layout_id: str | None = Query(default=None)) -> SimResult:
    result = services.get_latest_sim(project_id, layout_id)
    if result is None:
        raise HTTPException(404, "Bu layout için kayıtlı simülasyon yok.")
    return result


@router.get("/simulations/{run_id}", response_model=SimResult)
def get_simulation(run_id: int) -> SimResult:
    return services.get_simulation(run_id)


# ---- optimizasyon
@router.post("/projects/{project_id}/optimizations", status_code=201)
def run_optimization(project_id: str, config: OptimizationConfig | None = None) -> dict[str, Any]:
    opt_id, result = services.run_optimization_for(project_id, config)
    return {"optimization_id": opt_id, "result": result.model_dump(mode="json")}


@router.get("/projects/{project_id}/optimizations")
def list_optimizations(project_id: str) -> list[dict[str, Any]]:
    return services.list_optimizations(project_id)


@router.get("/projects/{project_id}/optimizations/latest", response_model=OptimizationResult)
def latest_optimization(project_id: str) -> OptimizationResult:
    found = services.get_latest_optimization(project_id)
    if found is None:
        raise HTTPException(404, "Kayıtlı optimizasyon yok.")
    return found[1]


@router.get("/optimizations/{opt_id}", response_model=OptimizationResult)
def get_optimization(opt_id: int) -> OptimizationResult:
    return services.get_optimization(opt_id)


@router.post("/projects/{project_id}/optimizations/{opt_id}/apply/{alternative_id}", response_model=Layout)
def apply_alternative(project_id: str, opt_id: int, alternative_id: str) -> Layout:
    return services.apply_optimization_alternative(project_id, opt_id, alternative_id)


@router.get("/projects/{project_id}/reports")
def reports(project_id: str) -> list[dict[str, Any]]:
    return services.list_reports(project_id)
