from pydantic import ConfigDict

from webapi.models.common import WebAPIModel


class SkillSummary(WebAPIModel):
    """One entry in ``GET /api/skills``. Schema matches
    ``tools.skills_tool._find_all_skills()`` which returns the parsed
    frontmatter of each ``SKILL.md``. Unknown frontmatter fields pass
    through opaquely (``extra="allow"``).
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str
    description: str | None = None
    category: str | None = None


class SkillsListResponse(WebAPIModel):
    success: bool
    skills: list[SkillSummary]
    categories: list[str] = []
    count: int | None = None
    hint: str | None = None
    message: str | None = None


class SkillCategoryItem(WebAPIModel):
    name: str
    skill_count: int
    description: str | None = None


class SkillCategoriesResponse(WebAPIModel):
    success: bool
    categories: list[SkillCategoryItem]
    hint: str | None = None
    message: str | None = None


class SkillDetailResponse(WebAPIModel):
    """``GET /api/skills/{name}`` — full skill body. ``tools.skills_tool.skill_view``
    returns an opaque dict (content, tags, related files, frontmatter …); we
    surface the common fields and allow extras to pass through.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    success: bool
    name: str | None = None
    content: str | None = None
    category: str | None = None
