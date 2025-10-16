from pydantic import BaseModel, Field
from typing import List

class ImportRequest(BaseModel):
    distribuidoras: List[str] = Field(..., example=["SULGIPE", "ENEL RJ"])
    anos: List[int] = Field(..., example=[2023, 2024])