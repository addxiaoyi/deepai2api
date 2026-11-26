"""离线测试：自动刷课（课程列表 / 章节 / 上报记录 / 积分变化）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "linekit"))

from amd_reg import cli, courses, site  # noqa: E402

COOKIE = "SESSION=ok"


class Backend:
    """复刻 AMD 学院接口的形状（字段 PascalCase）。"""

    def __init__(self):
        self.records: list[dict] = []
        self.started: list[str] = []
        self.withdrawn: list[str] = []
        self.points = 0
        self.study_status = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}

        if path.endswith("/User/GetCurrentUserDataSummary"):
            return httpx.Response(200, json={"code": 0, "data": {"nickName": "ada", "points": self.points}})
        if path.endswith("/Course/GetCourseList"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {"Id": "c-1", "Title": "ROCm 入门", "StudyRate": 0, "StudyStatus": 0},
                            {"Id": "c-2", "Title": "MI300X 调优", "StudyRate": 100, "StudyStatus": 3},
                        ]
                    },
                },
            )
        if path.endswith("/Course/GetCourseDetails"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "Id": "c-1",
                        "StudyRate": 100 if self.records else 0,
                        "StudyStatus": self.study_status,
                        "IsHaveExam": False,
                        "CourseChildren": [{"Id": "ch-1", "CourseChildMenuItems": [{"Id": "m-1"}, {"Id": "m-2"}]}],
                    },
                },
            )
        if path.endswith("/Course/StartCourseStudy"):
            self.started.append(body.get("CourseId", ""))
            return httpx.Response(200, json={"code": 0, "data": True})
        if path.endswith("/Course/AddCourseStudyRecord"):
            self.records.append(body)
            return httpx.Response(200, json={"code": 0, "data": True})
        if path.endswith("/Course/WithdrawCourseStudy"):
            self.withdrawn.append(body.get("CourseId", ""))
            self.study_status = 3          # 学完
            self.points += 10              # 拿到 Devpoints
            return httpx.Response(200, json={"code": 0, "data": True})
        if path.endswith("/User/GetUserPointsRecords"):
            return httpx.Response(200, json={"code": 0, "data": {"totalPoints": self.points, "list": []}})
        if path.endswith("/CourseExam/GetCourseExam"):
            assert request.url.params.get("courseChildId"), "取题要带 courseChildId"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "Data": {
                        "QuestionBankModels": [
                            {
                                "QuestionId": "q-1",
                                "QuestionName": "ROCm?",
                                "RightAnswerType": 0,
                                "QuestionAnswerModels": [{"Option": "A", "OptionDetails": "AMD"}],
                            }
                        ]
                    },
                },
            )
        return httpx.Response(404, text="not found")


def patch(monkeypatch, backend):
    real = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(backend.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched)


def test_list_courses_reads_pascal_case_fields(monkeypatch):
    backend = Backend()
    patch(monkeypatch, backend)
    rows = courses.list_courses(COOKIE)
    assert [row["id"] for row in rows] == ["c-1", "c-2"]
    assert rows[0]["title"] == "ROCm 入门"
    assert rows[1]["studyStatus"] == 3


def test_study_course_posts_records_with_exact_payload(monkeypatch):
    backend = Backend()
    patch(monkeypatch, backend)
    info = courses.study_course(COOKIE, "c-1", step=30, target_seconds=90, pause=0)

    assert backend.started == ["c-1"]
    assert backend.withdrawn == ["c-1"]
    # 2 个菜单项 × 3 次上报（90/30）
    assert len(backend.records) == 6
    first = backend.records[0]
    assert set(first) == {"CourseId", "CourseChildId", "CourseChildMenuItemId", "StudyTime", "CourseStudyTime"}
    assert first["CourseId"] == "c-1"
    assert first["CourseChildId"] == "ch-1"
    assert first["CourseChildMenuItemId"] == "m-1"
    assert first["StudyTime"] == 30
    assert first["CourseStudyTime"] == 30
    # 第二个菜单项进度独立累计
    assert backend.records[3]["CourseChildMenuItemId"] == "m-2"
    assert backend.records[3]["CourseStudyTime"] == 30
    assert info["calls"] == 6
    assert info["seconds"] == 180
    assert info["studyStatus"] == 3


def test_study_is_capped_by_max_calls(monkeypatch):
    backend = Backend()
    patch(monkeypatch, backend)
    info = courses.study_course(COOKIE, "c-1", step=30, target_seconds=3600, max_calls=3, pause=0)
    assert info["calls"] == 3
    assert len(backend.records) == 3


def test_study_cli_reports_points_delta(monkeypatch, tmp_path, capsys):
    backend = Backend()
    patch(monkeypatch, backend)
    cli.main(["import", "--cookie", COOKIE, "--email", "a@example.com", "--output", str(tmp_path), "--json"])
    capsys.readouterr()

    code = cli.main(
        ["study", "--output", str(tmp_path), "--json", "--step", "30", "--target", "60", "--pause", "0"]
    )
    events = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n") if line.strip()]
    assert code == 0
    assert any(e["type"] == "course" for e in events)
    done = [e for e in events if e["type"] == "done"]
    assert done and done[0]["gained"] == 10, done
    # 已完成的 c-2（studyStatus=3）不该被重刷
    assert all(r["CourseId"] == "c-1" for r in backend.records)


def test_exam_questions(monkeypatch):
    backend = Backend()
    patch(monkeypatch, backend)
    data = courses.exam_questions(COOKIE, "ch-1")
    assert data["questions"][0]["id"] == "q-1"
    assert data["questions"][0]["options"][0]["value"] == "A"
