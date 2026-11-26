"""离线测试：考试（取题 / 交卷报文 / 判卷回填 / 自动通过）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "linekit"))

from amd_reg import cli, courses  # noqa: E402

COOKIE = "SESSION=exam"

QUIZ = {
    "Data": {
        "QuestionBankModels": [
            {
                "QuestionId": "q-1",
                "QuestionName": "ROCm 是？",
                "RightAnswerType": 0,
                "QuestionAnswerModels": [
                    {"Option": "A", "OptionDetails": "AMD 的开源计算栈"},
                    {"Option": "B", "OptionDetails": "NVIDIA 的驱动"},
                ],
            },
            {
                "QuestionId": "q-2",
                "QuestionName": "MI300X 显存？（多选）",
                "RightAnswerType": 1,
                "QuestionAnswerModels": [
                    {"Option": "A", "OptionDetails": "192GB"},
                    {"Option": "B", "OptionDetails": "HBM3"},
                ],
            },
        ]
    }
}


class ExamBackend:
    """复刻考试接口：第一次判错但给出正确答案，第二次判对。"""

    def __init__(self):
        self.submits: list[dict] = []
        self.rounds = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/User/GetCurrentUserDataSummary"):
            return httpx.Response(200, json={"code": 0, "data": {"nickName": "ada", "points": 100}})

        if path.endswith("/CourseExam/GetCourseExam"):
            assert request.method == "GET", "取题是 GET"
            assert request.url.params.get("courseChildId") == "ch-1"
            return httpx.Response(200, json={"code": 0, **QUIZ})

        if path.endswith("/CourseExam/SetExamAnswer"):
            body = json.loads(request.content)
            self.submits.append(body)
            self.rounds += 1
            return httpx.Response(200, json={"code": 0, "Data": {"ok": True}})

        if path.endswith("/CourseExam/GetAnswerResult"):
            if self.rounds <= 1:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "Data": {
                            "IsPass": False,
                            "Score": 50,
                            "QuestionResults": [
                                {"QuestionId": "q-1", "IsRight": False, "RightAnswer": "A"},
                                {"QuestionId": "q-2", "IsRight": False, "RightAnswer": "A,B"},
                            ],
                        },
                    },
                )
            return httpx.Response(
                200,
                json={"code": 0, "Data": {"IsPass": True, "Score": 100, "QuestionResults": [
                    {"QuestionId": "q-1", "IsRight": True},
                    {"QuestionId": "q-2", "IsRight": True},
                ]}},
            )
        if path.endswith("/Course/GetCourseDetails"):
            return httpx.Response(200, json={"code": 0, "data": {"CourseChildren": [{"Id": "ch-1"}, {"Id": "ch-2"}]}})
        return httpx.Response(404, text="not found")


def patch(monkeypatch, backend):
    real = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(backend.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched)


def test_exam_questions_normalizes_model(monkeypatch):
    backend = ExamBackend()
    patch(monkeypatch, backend)
    quiz = courses.exam_questions(COOKIE, "ch-1")
    questions = quiz["questions"]
    assert [q["id"] for q in questions] == ["q-1", "q-2"]
    assert questions[0]["text"] == "ROCm 是？"
    assert questions[0]["type"] == 0 and questions[1]["type"] == 1
    assert [opt["value"] for opt in questions[0]["options"]] == ["A", "B"]
    assert questions[0]["options"][0]["label"] == "AMD 的开源计算栈"


def test_build_answers_uses_frontend_field_names():
    questions = courses.exam_questions.__globals__  # noqa: F841  (保持 import 有用)
    built = courses.build_answers(
        [
            {"id": "q-1", "text": "t1", "type": 0, "options": [{"value": "A"}, {"value": "B"}]},
            {"id": "q-2", "text": "t2", "type": 1, "options": [{"value": "A"}, {"value": "B"}]},
        ],
        "all",
    )
    assert built[0] == {
        "OrderId": 0,
        "QuestionId": "q-1",
        "QuestionMsg": "t1",
        "RightAnswer": "A,B",
        "QuestionType": 0,
        "hasProblem": False,
    }
    assert built[1]["RightAnswer"] == "A,B"


def test_submit_exam_payload_matches_frontend(monkeypatch):
    backend = ExamBackend()
    patch(monkeypatch, backend)
    answers = courses.build_answers([{"id": "q-1", "text": "t", "type": 0, "options": [{"value": "A"}]}])
    courses.submit_exam(COOKIE, "ch-1", answers)
    body = backend.submits[0]
    assert set(body) == {"CourseChildId", "QuestionAnswerRequests"}
    assert body["CourseChildId"] == "ch-1"
    assert set(body["QuestionAnswerRequests"][0]) == {
        "OrderId",
        "QuestionId",
        "QuestionMsg",
        "RightAnswer",
        "QuestionType",
        "hasProblem",
    }


def test_auto_exam_harvests_answers_and_passes(monkeypatch):
    backend = ExamBackend()
    patch(monkeypatch, backend)
    info = courses.auto_exam(COOKIE, "ch-1", rounds=3)

    assert info["ok"] is True
    assert info["passed"] is True
    assert info["rounds"] == 2, "第一轮判错 → 回填答案 → 第二轮通过"
    assert backend.rounds == 2
    # 回填的正确答案出现在第二次提交里
    second = backend.submits[1]["QuestionAnswerRequests"]
    assert second[0]["RightAnswer"] == "A"
    assert second[1]["RightAnswer"] == "A,B"


def test_auto_exam_stops_when_no_answers_leaked(monkeypatch):
    class NoLeak(ExamBackend):
        def handler(self, request):
            if request.url.path.endswith("/CourseExam/GetAnswerResult"):
                return httpx.Response(200, json={"code": 0, "Data": {"IsPass": False, "QuestionResults": []}})
            return super().handler(request)

    backend = NoLeak()
    patch(monkeypatch, backend)
    info = courses.auto_exam(COOKIE, "ch-1", rounds=3)
    assert info["passed"] is False
    assert backend.rounds == 1, "没有答案可捡时不该反复交卷"


def test_cli_exam_resolves_first_child(monkeypatch, tmp_path, capsys):
    backend = ExamBackend()
    patch(monkeypatch, backend)
    cli.main(["import", "--cookie", COOKIE, "--email", "a@example.com", "--output", str(tmp_path), "--json"])
    capsys.readouterr()
    code = cli.main(["exam", "--course", "c-1", "--output", str(tmp_path), "--json"])
    events = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n") if line.strip()]
    assert code == 0
    exam_events = [e for e in events if e.get("type") == "exam"]
    assert exam_events, events
    assert exam_events[0]["child"] == "ch-1", "没给 --child 时取第一个章节"
    assert exam_events[0]["questions"] == 2
