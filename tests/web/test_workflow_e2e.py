"""One end-to-end test driving the whole workflow through HTTP only (no direct service calls):
member creates and submits -> reviewer approves -> the ceiling shows -> amendment -> completion ->
reviewer approves -> the project appears in the gallery.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import MEMBER_SUB, REVIEWER_SUB, get_csrf_token


def test_full_workflow_through_http(client: TestClient, login_as) -> None:
    # --- Member creates a draft proposal --------------------------------------------------------
    login_as(MEMBER_SUB)
    new_form = client.get("/projects/new")
    csrf = get_csrf_token(new_form.text)

    create_response = client.post(
        "/projects/new",
        data={
            "csrf_token": csrf,
            "title": "Ganymede Rover",
            "repo_url": "https://github.com/patchworklabs/rover",
            "write_up": "A rover for Ganymede.\nSecond line.",
            "budget_requested": "1,000.00",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    project_url = create_response.headers["location"]

    # --- Submit it for review --------------------------------------------------------------------
    detail = client.get(project_url)
    assert "Ganymede Rover" in detail.text
    csrf = get_csrf_token(detail.text)
    submit_response = client.post(f"{project_url}/submit", data={"csrf_token": csrf}, follow_redirects=False)
    assert submit_response.status_code == 303

    pending_page = client.get(project_url)
    assert "Pending review" in pending_page.text

    # --- Reviewer approves it ---------------------------------------------------------------------
    login_as(REVIEWER_SUB)
    review_page = client.get(project_url)
    assert "Needs 1 more approval." in review_page.text
    csrf = get_csrf_token(review_page.text)
    approve_response = client.post(
        f"{project_url}/review", data={"csrf_token": csrf, "decision": "approve"}, follow_redirects=False
    )
    assert approve_response.status_code == 303

    # --- The ceiling now shows -------------------------------------------------------------------
    approved_page = client.get(project_url)
    assert "Approved" in approved_page.text
    assert "$1,000.00" in approved_page.text

    # --- Submitter amends the budget down -----------------------------------------------------
    login_as(MEMBER_SUB)
    amend_page = client.get(project_url)
    assert "Start amendment" in amend_page.text
    csrf = get_csrf_token(amend_page.text)
    amend_start = client.post(f"{project_url}/amend", data={"csrf_token": csrf}, follow_redirects=False)
    assert amend_start.status_code == 303
    assert amend_start.headers["location"] == f"{project_url}/edit"

    edit_page = client.get(amend_start.headers["location"])
    csrf = get_csrf_token(edit_page.text)
    amend_save = client.post(
        f"{project_url}/edit",
        data={
            "csrf_token": csrf,
            "title": "Ganymede Rover",
            "repo_url": "https://github.com/patchworklabs/rover",
            "write_up": "A rover for Ganymede.",
            "budget_requested": "600.00",
        },
        follow_redirects=False,
    )
    assert amend_save.status_code == 303

    submit_amend_page = client.get(project_url)
    csrf = get_csrf_token(submit_amend_page.text)
    submit_amend = client.post(f"{project_url}/submit", data={"csrf_token": csrf}, follow_redirects=False)
    assert submit_amend.status_code == 303

    # Amendments leave the project approved while it's reviewed.
    still_approved_page = client.get(project_url)
    assert "Approved" in still_approved_page.text

    login_as(REVIEWER_SUB)
    review_amend_page = client.get(project_url)
    csrf = get_csrf_token(review_amend_page.text)
    approve_amend = client.post(
        f"{project_url}/review", data={"csrf_token": csrf, "decision": "approve"}, follow_redirects=False
    )
    assert approve_amend.status_code == 303

    after_amend_page = client.get(project_url)
    assert "$600.00" in after_amend_page.text

    # --- Submitter starts and submits completion --------------------------------------------------
    login_as(MEMBER_SUB)
    detail_page = client.get(project_url)
    csrf = get_csrf_token(detail_page.text)
    complete_start = client.post(f"{project_url}/complete", data={"csrf_token": csrf}, follow_redirects=False)
    assert complete_start.status_code == 303

    completion_edit_page = client.get(complete_start.headers["location"])
    assert "screenshot-widget" in completion_edit_page.text  # screenshot upload widget
    csrf = get_csrf_token(completion_edit_page.text)
    completion_save = client.post(
        f"{project_url}/edit",
        data={
            "csrf_token": csrf,
            "title": "Ganymede Rover",
            "repo_url": "https://github.com/patchworklabs/rover",
            "write_up": "Final write-up for the rover.",
            "budget_requested": "600.00",
            "demo_url": "https://example.com/rover-demo",
            "tags": "rover, ganymede",
            "credited_builder_emails": "",
        },
        follow_redirects=False,
    )
    assert completion_save.status_code == 303

    submit_completion_page = client.get(project_url)
    csrf = get_csrf_token(submit_completion_page.text)
    submit_completion_response = client.post(
        f"{project_url}/submit-completion", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert submit_completion_response.status_code == 303

    pending_completion_page = client.get(project_url)
    assert "Pending completion review" in pending_completion_page.text

    # --- Reviewer approves the completion ----------------------------------------------------------
    login_as(REVIEWER_SUB)
    completion_review_page = client.get(project_url)
    csrf = get_csrf_token(completion_review_page.text)
    approve_completion = client.post(
        f"{project_url}/review", data={"csrf_token": csrf, "decision": "approve"}, follow_redirects=False
    )
    assert approve_completion.status_code == 303

    completed_page = client.get(project_url)
    assert "Completed" in completed_page.text

    # --- The project now appears in the public gallery, with no auth --------------------------------
    anonymous_client = TestClient(client.app)
    gallery_index = anonymous_client.get("/gallery")
    assert gallery_index.status_code == 200
    assert "Ganymede Rover" in gallery_index.text

    gallery_detail = anonymous_client.get(project_url.replace("/projects/", "/gallery/"))
    assert gallery_detail.status_code == 200
    assert "Final write-up for the rover." in gallery_detail.text
    assert "example.com/rover-demo" in gallery_detail.text
    assert "rover" in gallery_detail.text
