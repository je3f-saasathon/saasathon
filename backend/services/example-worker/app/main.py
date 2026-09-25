import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="example-worker")

SERVICE_SECRET_ENV = "SERVICE_SECRET"


def require_service_secret(x_service_secret: str | None = Header(default=None)) -> None:
    """Shared-secret check. Every route except /health must call this.

    The caller (backend) sends the shared secret in the X-Service-Secret
    header. The secret itself lives only in each side's env and is never
    embedded in code.
    """
    expected = os.environ.get(SERVICE_SECRET_ENV)
    if not expected:
        raise HTTPException(status_code=500, detail="service secret not configured")
    if x_service_secret != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-Service-Secret header")


class ExampleRequest(BaseModel):
    text: str


class ExampleResponse(BaseModel):
    result: str
    length: int


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}


@app.post("/v1/example", response_model=ExampleResponse, dependencies=[Depends(require_service_secret)])
def example(payload: ExampleRequest) -> ExampleResponse:
    """Stub transform: uppercases the input text and reports its length.

    Replace this handler's body with real logic when this template is
    copied into a purpose-built service.
    """
    return ExampleResponse(result=payload.text.upper(), length=len(payload.text))
