from ninja import Schema


class JevQuestionIn(Schema):
    type: str  # "noul" | "choice" | "score"
    instructions: str
    criteria: dict[str, str] | list[str]


class JevRunIn(Schema):
    state: str
    questions: dict[str, JevQuestionIn]


class JevRunOut(Schema):
    model: str
    answers: dict
    usage: dict
