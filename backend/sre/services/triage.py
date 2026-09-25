from ..llm.clients import JevClient, client_for
from ..llm.resolve import get_llm_config
from ..models import IncidentRun, PipelineStep
from ..temporal_types import AnomalyResult, Classification
from .context import UNTRUSTED_NOTICE, heuristic_keywords, incident_context

CATEGORIES = {
    "null_reference": "Null/None/undefined access",
    "timeout": "Timeouts or slow calls",
    "database": "Query, migration, constraint or connection errors",
    "dependency_failure": "A downstream service or third-party API failed",
    "configuration": "Missing or wrong config, env var or secret",
    "validation": "Bad input not handled",
    "resource_exhaustion": "Memory, disk, file handles, rate limits",
    "logic_error": "Wrong behavior in application code",
    "other": "None of the above",
}
SEVERITIES = ("low", "medium", "high", "critical")


class AnomalyChecker:
    """Second opinion on Uptrace's alert: is this real and worth an engineer's time?"""

    SYSTEM = (
        "You are an experienced SRE triaging an alert. Decide whether it is a real, "
        "actionable production problem, or noise (expected/handled errors, flaky tests, "
        "one-off transient blips, bots). " + UNTRUSTED_NOTICE +
        ' Reply with JSON only: {"is_anomaly": true|false, "reasoning": "<one paragraph>"}'
    )

    def __init__(self, run: IncidentRun):
        self.run = run
        self.client = client_for(get_llm_config(run.project, PipelineStep.ANOMALY_DOUBLE_CHECK))

    def check(self) -> AnomalyResult:
        context = incident_context(self.run)
        if isinstance(self.client, JevClient):
            choice = self.client.choose(
                context,
                "Is this a real, actionable production anomaly rather than noise?",
                {"yes": "Real, actionable problem", "no": "Noise or expected behavior"},
                name="anomaly_double_check",
            )
            return AnomalyResult(is_anomaly=choice == "yes", reasoning=f"Jev answered '{choice}'")
        data = self.client.complete_json(self.SYSTEM, context, name="anomaly_double_check")
        return AnomalyResult(
            is_anomaly=bool(data.get("is_anomaly")), reasoning=str(data.get("reasoning", ""))
        )


class BugClassifier:
    SYSTEM = (
        "You are an SRE classifying a production bug so it can be matched to a remediation "
        "playbook. " + UNTRUSTED_NOTICE + " Reply with JSON only: "
        '{"category": one of ' + ", ".join(f'"{c}"' for c in CATEGORIES) + ", "
        '"severity": one of "low","medium","high","critical", '
        '"summary": "<one sentence>", '
        '"keywords": ["<5-10 lowercase search terms: exception type, module, function, '
        'error codes>"], "suspected_files": ["<repo paths, if the trace shows them>"]}'
    )

    def __init__(self, run: IncidentRun):
        self.run = run
        self.client = client_for(get_llm_config(run.project, PipelineStep.BUG_CLASSIFICATION))

    def classify(self) -> Classification:
        context = incident_context(self.run)
        if isinstance(self.client, JevClient):
            category = self.client.choose(
                context, "Which category best describes this bug?", CATEGORIES,
                name="bug_classification",
            )
            return Classification(
                category=category if category in CATEGORIES else "other",
                severity="medium",
                summary=f"Classified by Jev as {category}",
                keywords=heuristic_keywords(self.run),
            )
        data = self.client.complete_json(self.SYSTEM, context, name="bug_classification")
        category = data.get("category")
        severity = data.get("severity")
        keywords = [str(k).lower().strip() for k in data.get("keywords") or [] if str(k).strip()]
        return Classification(
            category=category if category in CATEGORIES else "other",
            severity=severity if severity in SEVERITIES else "medium",
            summary=str(data.get("summary", ""))[:500],
            keywords=keywords[:15] or heuristic_keywords(self.run),
            suspected_files=[str(f) for f in data.get("suspected_files") or []][:20],
        )
