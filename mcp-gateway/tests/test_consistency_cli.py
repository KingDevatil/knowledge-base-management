import pytest

from src import consistency_cli


def test_format_text_report_includes_stats_and_issues():
    report = consistency_cli.format_text_report(
        {
            "success": False,
            "issues": [
                {
                    "severity": "error",
                    "code": "missing_source",
                    "doc_id": "doc-1",
                    "message": "Document source file is missing or unreadable.",
                }
            ],
            "stats": {
                "indexed_documents": 1,
                "chroma_documents": 1,
                "errors": 1,
                "warnings": 0,
            },
        }
    )

    assert "Knowledge base consistency report" in report
    assert "success: False" in report
    assert "[error] missing_source doc_id=doc-1" in report


@pytest.mark.parametrize(
    ("stats", "fail_on_warning", "expected"),
    [
        ({"errors": 1, "warnings": 0}, False, 1),
        ({"errors": 0, "warnings": 1}, False, 0),
        ({"errors": 0, "warnings": 1}, True, 1),
        ({"errors": 0, "warnings": 0}, True, 0),
    ],
)
def test_exit_code_for(stats, fail_on_warning, expected):
    assert consistency_cli.exit_code_for({"stats": stats}, fail_on_warning) == expected


def test_main_prints_json(monkeypatch, capsys):
    async def fake_run_consistency_check():
        return {
            "success": True,
            "issue_count": 0,
            "issues": [],
            "stats": {
                "indexed_documents": 0,
                "chroma_documents": 0,
                "errors": 0,
                "warnings": 0,
            },
        }

    monkeypatch.setattr(consistency_cli, "run_consistency_check", fake_run_consistency_check)

    exit_code = consistency_cli.main(["--json"])

    assert exit_code == 0
    assert '"success": true' in capsys.readouterr().out
