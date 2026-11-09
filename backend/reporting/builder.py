"""[Frontend&Reporting Engineer] Rapor üretimi."""
from pathlib import Path

from backend.schemas import OptimizationResult, Project, SimResult


def build_report_context(project: Project, baseline_sim: SimResult, opt: OptimizationResult | None, ai_text: str | None = None) -> dict:
    """14 bölümlük bağlam; TÜM sayılar kaynak nesnelerden okunur (hiçbir sayı elle yazılmaz)."""
    raise NotImplementedError


def export_html(ctx: dict, out_dir: Path) -> Path: raise NotImplementedError
def export_pdf(ctx: dict, out_dir: Path) -> Path: raise NotImplementedError
def export_excel(ctx: dict, out_dir: Path) -> Path: raise NotImplementedError
def export_png(ctx: dict, out_dir: Path) -> list[Path]: raise NotImplementedError
