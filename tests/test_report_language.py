from app.report_engine import ReportEngine


def test_chinese_report_deductions_never_fall_back_to_plain_english() -> None:
    generated = ["The answer did not explain the failure boundary."]

    assert ReportEngine._localized_deductions(
        generated,
        ["未说明故障边界和验证依据"],
        english=False,
        scorable=True,
    ) == ["未说明故障边界和验证依据"]
    assert ReportEngine._localized_deductions(
        generated,
        [],
        english=False,
        scorable=True,
    ) == ["回答缺少可验证的关键细节"]
    assert ReportEngine._localized_deductions(
        generated,
        [],
        english=True,
        scorable=True,
    ) == generated
