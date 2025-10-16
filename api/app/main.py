import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.imports import router as imports_router

app = FastAPI(title="Youon API")

origins = [o.strip() for o in os.getenv("ALLOW_ORIGINS", "*").split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(imports_router)

@app.get("/v1/ping")
def ping():
    return {"ok": True}
