"""自动刷课：把 AMD 学院（Academy）的课程看完，拿 Devpoints 换 GPU 小时。

实测依据（`assets/scorm-deliver-*.js` 与 `course-detail-*.js` 的真实调用）：

    // 播放器每累积 ≥1 秒就上报一次
    await ot({
      CourseId: r,                 // 课程
      CourseChildId: l,            // 子课程（章节）
      CourseChildMenuItemId: x,    // 章节下的条目（SCORM 菜单项）
      StudyTime: Math.max(1, Math.round(b)),   // 本次新增学习秒数
      CourseStudyTime: R,          // 当前播放进度（累计秒）
    })

其余相关端点（同一个 bundle）：

    POST /api/Course/GetCourseList          {PageIndex, PageSize, ...}      课程列表
    GET  /api/Course/GetCourseDetails       ?courseId=                      课程详情（StudyRate/StudyStatus）
    GET  /api/Course/GetCourseChildDetails  ?courseId=&childId=             章节 + 菜单项
    POST /api/Course/StartCourseStudy       {CourseId,...}                  开始学习
    POST /api/Course/AddCourseStudyRecord   {CourseId,CourseChildId,CourseChildMenuItemId,StudyTime,CourseStudyTime}
    POST /api/Course/WithdrawCourseStudy    {CourseId,...}                  结束学习
    POST /api/CourseExam/GetCourseExam      {CourseId}                      考试（若有）
    POST /api/CourseExam/SetExamAnswer      {...}                           交卷
    POST /api/User/GetUserPointsRecords     {PageIndex,PageSize}            Devpoints 流水

要点：字段是 **PascalCase**；`StudyTime` 是"本次新增秒数"，`CourseStudyTime` 是"累计进度"。
所以刷课不用等真实播放 —— 按 `StudyTime` 增量循环上报即可，但**别把增量调得离谱**
（一次 3600 秒明显异常），默认按 30 秒/次、每章节到目标进度为止。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from linekit.net import build_client, request
from linekit.errors import LineError

from . import site


def _client(proxy: str = "", timeout: float = 30.0):
    return build_client(proxy=proxy, timeout=timeout)


def _call(cookie: str, path: str, *, method: str = "GET", body: Any = None, params: dict[str, Any] | None = None,
          token: str = "", timeout: float = 30.0, proxy: str = "") -> Any:
    client = _client(proxy=proxy, timeout=timeout)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    try:
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        if params:
            kwargs["params"] = params
        response = request(client, method, f"{site.API}{path}", **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": (response.text or "")[:300]}
        if response.status_code >= 400:
            raise LineError(f"{path}: HTTP {response.status_code}", detail=payload)
        return payload
    finally:
        client.close()


# --------------------------------------------------------------------- 课程


def list_courses(cookie: str, *, token: str = "", page_size: int = 50, proxy: str = "") -> list[dict[str, Any]]:
    payload = _call(
        cookie,
        "/Course/GetCourseList",
        method="POST",
        body={"PageIndex": 1, "PageSize": page_size},
        token=token,
        proxy=proxy,
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("list") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "id": str(row.get("Id") or row.get("CourseId") or row.get("id") or ""),
                "title": str(row.get("Title") or row.get("title") or row.get("Name") or ""),
                "studyRate": row.get("StudyRate", row.get("studyRate")),
                "studyStatus": row.get("StudyStatus", row.get("studyStatus")),
                "isHaveExam": bool(row.get("IsHaveExam") or row.get("Survey") or False),
                "isPassExam": bool(row.get("IsPassExam") or False),
            }
        )
    return [row for row in out if row["id"]]


def course_detail(cookie: str, course_id: str, *, token: str = "", proxy: str = "") -> dict[str, Any]:
    payload = _call(
        cookie, "/Course/GetCourseDetails", params={"courseId": course_id}, token=token, proxy=proxy
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def child_details(cookie: str, course_id: str, child_id: str = "", *, token: str = "", proxy: str = "") -> dict[str, Any]:
    payload = _call(
        cookie,
        "/Course/GetCourseChildDetails",
        params={"courseId": course_id, **({"childId": child_id} if child_id else {})},
        token=token,
        proxy=proxy,
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def start(cookie: str, course_id: str, *, token: str = "", proxy: str = "") -> Any:
    return _call(cookie, "/Course/StartCourseStudy", method="POST", body={"CourseId": course_id}, token=token, proxy=proxy)


def withdraw(cookie: str, course_id: str, *, token: str = "", proxy: str = "") -> Any:
    return _call(
        cookie, "/Course/WithdrawCourseStudy", method="POST", body={"CourseId": course_id}, token=token, proxy=proxy
    )


def add_record(
    cookie: str,
    *,
    course_id: str,
    child_id: str,
    menu_item_id: str,
    study_time: int,
    course_study_time: int,
    token: str = "",
    proxy: str = "",
) -> Any:
    """上报一次学习记录 —— 字段名与前端逐字一致（PascalCase）。"""
    body = {
        "CourseId": course_id,
        "CourseChildId": child_id,
        "CourseChildMenuItemId": menu_item_id,
        "StudyTime": max(1, int(study_time)),
        "CourseStudyTime": max(0, int(course_study_time)),
    }
    return _call(cookie, "/Course/AddCourseStudyRecord", method="POST", body=body, token=token, proxy=proxy)


def points_delta(cookie: str, *, token: str = "", proxy: str = "") -> dict[str, Any]:
    payload = _call(
        cookie, "/User/GetUserPointsRecords", method="POST", body={"PageIndex": 1, "PageSize": 10}, token=token, proxy=proxy
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("list") if isinstance(data, dict) else data
    total = None
    if isinstance(data, dict):
        for field in ("totalPoints", "points", "TotalPoints"):
            if isinstance(data.get(field), (int, float)):
                total = data[field]
                break
    return {"total": total, "rows": rows if isinstance(rows, list) else []}


def _children_of(detail: dict[str, Any]) -> list[dict[str, Any]]:
    children = detail.get("CourseChildren") or detail.get("courseChildren") or []
    return [row for row in children if isinstance(row, dict)]


def _items_of(child: dict[str, Any]) -> list[dict[str, Any]]:
    items = (
        child.get("CourseChildMenuItems")
        or child.get("MenuItems")
        or child.get("Items")
        or child.get("Children")
        or []
    )
    return [row for row in items if isinstance(row, dict)]


def study_course(
    cookie: str,
    course_id: str,
    *,
    token: str = "",
    step: int = 30,
    target_seconds: int = 3600,
    max_calls: int = 400,
    pause: float = 1.0,
    proxy: str = "",
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """把一个课程的所有章节 / 菜单项刷到 `target_seconds` 累计进度。

    返回 `{course, children, calls, seconds, status}`，status 取自课程详情的 StudyStatus/StudyRate。
    """
    say = log or (lambda _m: None)
    start(cookie, course_id, token=token, proxy=proxy)
    detail = course_detail(cookie, course_id, token=token, proxy=proxy)
    children = _children_of(detail)
    if not children:
        # 有些课程没有子章节：直接对课程本身报记录
        child = child_details(cookie, course_id, token=token, proxy=proxy)
        children = _children_of(child) or [{"Id": course_id, "CourseChildMenuItems": [{"Id": ""}]}]

    calls = 0
    seconds = 0
    done_children = 0
    for child in children:
        child_id = str(child.get("Id") or child.get("CourseChildId") or "")
        if not child_id:
            continue
        items = _items_of(child) or [{"Id": ""}]
        for item in items:
            menu_id = str(item.get("Id") or item.get("CourseChildMenuItemId") or "")
            progress = 0
            while progress < target_seconds and calls < max_calls:
                add_record(
                    cookie,
                    course_id=course_id,
                    child_id=child_id,
                    menu_item_id=menu_id,
                    study_time=step,
                    course_study_time=progress + step,
                    token=token,
                    proxy=proxy,
                )
                progress += step
                seconds += step
                calls += 1
                if pause:
                    time.sleep(pause)
        done_children += 1
        say(f"章节 {child_id} 完成（{progress}s × {len(items)} 项）")

    try:
        withdraw(cookie, course_id, token=token, proxy=proxy)
    except LineError:
        pass

    final = course_detail(cookie, course_id, token=token, proxy=proxy)
    return {
        "course": course_id,
        "children": done_children,
        "calls": calls,
        "seconds": seconds,
        "studyRate": final.get("StudyRate"),
        "studyStatus": final.get("StudyStatus"),
        "isHaveExam": bool(final.get("IsHaveExam")),
        "isPassExam": bool(final.get("IsPassExam")),
    }



# ------------------------------------------------------------------ 考试
#
# 从 `assets/course-detail-*.js` 的 quizDialog 里逐字对出来的契约：
#
#   GET  api/CourseExam/GetCourseExam?courseChildId=<child>
#        -> { Data: { QuestionBankModels: [ { QuestionId, QuestionName, RightAnswerType,
#                                            QuestionAnswerModels: [ { Option, OptionDetails } ] } ] } }
#   POST api/CourseExam/SetExamAnswer
#        { CourseChildId, QuestionAnswerRequests: [ { OrderId, QuestionId, QuestionMsg,
#                                                    RightAnswer, QuestionType, hasProblem } ] }
#        （RightAnswer 是**逗号分隔的 Option 值**；RightAnswerType 0=单选 1=多选）
#   GET  api/CourseExam/GetAnswerResult?courseChildId=<child>
#        -> { Data: { QuestionResults: [...] } }
#
# 前端还会在交卷前校验"每题都有答案"，所以自动答题必须给每题都填值。


def exam_questions(cookie: str, child_id: str, *, token: str = "", proxy: str = "") -> dict[str, Any]:
    """取题：注意是 **GET + courseChildId**（不是 POST + CourseId）。"""
    payload = _call(
        cookie, "/CourseExam/GetCourseExam", params={"courseChildId": child_id}, token=token, proxy=proxy
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload.get("Data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"questions": [], "raw": payload}

    raw = data.get("QuestionBankModels") or data.get("QuestionModels") or []
    questions: list[dict[str, Any]] = []
    for index, row in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(row, dict):
            continue
        options = row.get("QuestionAnswerModels") or row.get("AnswerModels") or []
        questions.append(
            {
                "order": index,
                "id": str(row.get("QuestionId") or ""),
                "text": str(row.get("QuestionName") or ""),
                "type": int(row.get("RightAnswerType") or 0),
                "options": [
                    {
                        "value": str(opt.get("Option") or ""),
                        "label": str(opt.get("OptionDetails") or opt.get("Option") or ""),
                    }
                    for opt in (options if isinstance(options, list) else [])
                    if isinstance(opt, dict)
                ],
            }
        )
    return {"questions": questions, "raw": data}


def build_answers(questions: list[dict[str, Any]], strategy: str = "first") -> list[dict[str, Any]]:
    """按策略生成 `QuestionAnswerRequests`（字段名与前端一致）。"""
    out: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        options = [opt["value"] for opt in question.get("options", []) if opt.get("value")]
        if not options:
            picked = ""
        elif strategy == "all":
            picked = ",".join(options)
        else:
            picked = options[0]
        out.append(
            {
                "OrderId": index,
                "QuestionId": question["id"],
                "QuestionMsg": question.get("text", ""),
                "RightAnswer": picked,
                "QuestionType": question.get("type", 0),
                "hasProblem": False,
            }
        )
    return out


def submit_exam(cookie: str, child_id: str, answers: list[dict[str, Any]], *, token: str = "",
                proxy: str = "") -> Any:
    """交卷：报文与前端逐字一致。"""
    body = {"CourseChildId": child_id, "QuestionAnswerRequests": answers}
    return _call(cookie, "/CourseExam/SetExamAnswer", method="POST", body=body, token=token, proxy=proxy)


def exam_result(cookie: str, child_id: str, *, token: str = "", proxy: str = "") -> dict[str, Any]:
    payload = _call(
        cookie, "/CourseExam/GetAnswerResult", params={"courseChildId": child_id}, token=token, proxy=proxy
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload.get("Data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def _harvest_answers(result: dict[str, Any], question_ids: list[str]) -> dict[str, str]:
    """从判卷结果里捡正确答案（不同站字段名不一，能捡到就捡）。"""
    found: dict[str, str] = {}
    rows = result.get("QuestionResults") or result.get("QuestionResultModels") or []
    if not isinstance(rows, list):
        return found
    for row in rows:
        if not isinstance(row, dict):
            continue
        qid = str(row.get("QuestionId") or "")
        if not qid or (question_ids and qid not in question_ids):
            continue
        for field in ("RightAnswer", "CorrectAnswer", "StandardAnswer", "Answer"):
            value = row.get(field)
            if isinstance(value, str) and value.strip():
                found[qid] = value.strip()
                break
    return found


def _passed(result: dict[str, Any]) -> bool | None:
    for field in ("IsPass", "IsPassExam", "Passed", "isPass"):
        value = result.get(field)
        if isinstance(value, bool):
            return value
    score = result.get("Score", result.get("TotalScore"))
    if isinstance(score, (int, float)):
        # 没有明确的 pass 标记时，按"有没有答错"判断
        rows = result.get("QuestionResults") or []
        if isinstance(rows, list) and rows:
            return all(bool(row.get("IsRight", row.get("IsCorrect", False))) for row in rows if isinstance(row, dict))
        return score > 0
    return None


def auto_exam(
    cookie: str,
    child_id: str,
    *,
    strategy: str = "first",
    rounds: int = 3,
    token: str = "",
    proxy: str = "",
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """自动答题：先按策略交一次 → 读判卷结果 → 捡到正确答案就再交 → 最多 `rounds` 轮。"""
    say = log or (lambda _m: None)
    quiz = exam_questions(cookie, child_id, token=token, proxy=proxy)
    questions = quiz.get("questions") or []
    if not questions:
        return {"ok": False, "reason": "no_questions"}
    question_ids = [q["id"] for q in questions]

    answers = build_answers(questions, strategy)
    history: list[dict[str, Any]] = []
    passed: bool | None = None
    for round_no in range(1, max(1, rounds) + 1):
        submit_exam(cookie, child_id, answers, token=token, proxy=proxy)
        result = exam_result(cookie, child_id, token=token, proxy=proxy)
        passed = _passed(result)
        history.append({"round": round_no, "passed": passed, "score": result.get("Score")})
        say(f"第 {round_no} 轮：passed={passed}")
        if passed:
            break
        harvested = _harvest_answers(result, question_ids)
        if not harvested:
            say("判卷结果里没有正确答案，停止重试")
            break
        changed = False
        for answer in answers:
            better = harvested.get(answer["QuestionId"])
            if better and better != answer["RightAnswer"]:
                answer["RightAnswer"] = better
                changed = True
        if not changed:
            break
    return {
        "ok": True,
        "questions": len(questions),
        "rounds": len(history),
        "passed": passed,
        "history": history,
        "answers": answers,
    }
