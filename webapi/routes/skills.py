import json

from fastapi import APIRouter, HTTPException, Query

from tools.skills_tool import skill_view, skills_categories, skills_list
from webapi.models.skills import (
    SkillCategoriesResponse,
    SkillDetailResponse,
    SkillsListResponse,
)


router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("", response_model=SkillsListResponse)
async def list_skills(category: str | None = Query(None)) -> SkillsListResponse:
    result = json.loads(skills_list(category=category))
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to list skills"))
    return SkillsListResponse.model_validate(result)


@router.get("/categories", response_model=SkillCategoriesResponse)
async def list_skill_categories() -> SkillCategoriesResponse:
    result = json.loads(skills_categories())
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to list skill categories"))
    return SkillCategoriesResponse.model_validate(result)


@router.get("/{name:path}", response_model=SkillDetailResponse)
async def get_skill(
    name: str,
    file_path: str | None = Query(None),
) -> SkillDetailResponse:
    result = json.loads(skill_view(name=name, file_path=file_path))
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", f"Skill '{name}' not found"))
    return SkillDetailResponse.model_validate(result)
