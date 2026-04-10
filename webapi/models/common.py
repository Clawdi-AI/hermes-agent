from pydantic import BaseModel, ConfigDict


class WebAPIModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class OkResponse(WebAPIModel):
    """Minimal acknowledgement envelope for mutation endpoints without a body."""

    ok: bool = True
