"""Smoke test: pydantic v2 BaseModel validation and serialization."""

from pydantic import BaseModel, Field


class SampleModel(BaseModel):
    id: str
    content: str = Field(min_length=1)
    importance: float = Field(ge=0.0, le=1.0)


def test_pydantic_validation_ok():
    m = SampleModel(id="abc", content="hello", importance=0.5)
    assert m.id == "abc"


def test_pydantic_validation_fail():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SampleModel(id="abc", content="", importance=1.5)


def test_pydantic_json_roundtrip():
    m = SampleModel(id="abc", content="hello", importance=0.5)
    s = m.model_dump_json()
    m2 = SampleModel.model_validate_json(s)
    assert m == m2
