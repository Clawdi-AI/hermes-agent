import json

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from tools.skills_tool import skill_view, skills_categories, skills_list
from webapi.models.skills import (
    SkillCategoriesResponse,
    SkillDetailResponse,
    SkillsListResponse,
)


router = APIRouter(prefix="/api/skills", tags=["skills"])


# ``tools.skills_tool.*`` walks the on-disk skills tree (``os.listdir``,
# ``Path.read_text``) on every call. Wrap in a threadpool so the disk
# IO doesn't pin the event loop while a slow filesystem is enumerating
# hundreds of skill markdown files.


@router.get("", response_model=SkillsListResponse)
async def list_skills(category: str | None = Query(None)) -> SkillsListResponse:
    raw = await run_in_threadpool(skills_list, category=category)
    result = json.loads(raw)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to list skills"))
    return SkillsListResponse.model_validate(result)


@router.get("/categories", response_model=SkillCategoriesResponse)
async def list_skill_categories() -> SkillCategoriesResponse:
    raw = await run_in_threadpool(skills_categories)
    result = json.loads(raw)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to list skill categories"))
    return SkillCategoriesResponse.model_validate(result)


@router.get("/{name:path}", response_model=SkillDetailResponse)
async def get_skill(
    name: str,
    file_path: str | None = Query(None),
) -> SkillDetailResponse:
    raw = await run_in_threadpool(skill_view, name=name, file_path=file_path)
    result = json.loads(raw)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", f"Skill '{name}' not found"))
    return SkillDetailResponse.model_validate(result)
