from django.http import HttpRequest
from ninja import Router

from .client import JevError, run_jev
from .schemas import JevRunIn, JevRunOut

router = Router(tags=["jev"])


@router.post("/run", response={200: JevRunOut, 502: dict})
def run(request: HttpRequest, payload: JevRunIn):
    try:
        result = run_jev(
            payload.state,
            {name: q.dict() for name, q in payload.questions.items()},
        )
    except JevError as exc:
        return 502, {"detail": str(exc)}
    return 200, result
