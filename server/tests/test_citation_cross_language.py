"""C07 回归：跨语言支持度判定（中英术语表归一，阈值不降）。"""

from app.citation import bind_claims_to_evidence
from app.models import RagEvidence

EIA_EN = RagEvidence(
    doc_id="news_event:evt_oil_policy_eia_record",
    doc_type="news_event_cluster",
    source_id="eia_press",
    tier="A",
    title="EIA Press Release (07/07/2026): EIA increases U.S. crude production forecast",
    summary="EIA expects U.S. crude production to reach a record in 2026.",
    observed_at="2026-07-07",
    visible_at="2026-07-07",
)

CN_SUMMARY = RagEvidence(
    doc_id="news_event:evt_oil_policy_cn_summary",
    doc_type="news_event_cluster",
    source_id="texnet_polyester_news",
    tier="B",
    title="9月11日涤纶POY为9242.50",
    summary="涤纶POY参考价上行，下游按需采购。",
    observed_at="2026-09-11",
    visible_at="2026-09-11",
)

UNRELATED = RagEvidence(
    doc_id="news_event:evt_unrelated",
    doc_type="news_event_cluster",
    source_id="test",
    tier="C",
    title="Chocolate prices stabilize ahead of holiday season",
    summary="Cocoa supply weighs on confectionery margins.",
    observed_at="2026-09-11",
    visible_at="2026-09-11",
)


def _bind(claim: str, evidence: RagEvidence):
    return bind_claims_to_evidence([claim], [evidence], minimum_score=0.35)[0]


def test_cross_commodity_claim_rejected() -> None:
    # Codex 复核：证据谈 POY，论断谈巧克力——共同措辞"价格上涨"不构成品种支持。
    evidence = RagEvidence(
        doc_id="news_event:evt_poy_sep11",
        doc_type="news_event_cluster",
        source_id="texnet_polyester_news", tier="B",
        title="2026年9月11日POY价格上涨",
        summary="POY价格上涨，下游按需采购。",
        observed_at="2026-09-11", visible_at="2026-09-11",
    )
    binding = _bind("巧克力价格上涨", evidence)
    assert binding.supported is False


def test_cross_language_chinese_claim_matches_english_evidence() -> None:
    binding = _bind("EIA预测美国原油产量创纪录。", EIA_EN)
    assert binding.supported is True
    assert binding.doc_ids == (EIA_EN.doc_id,)


def test_cross_language_english_claim_matches_chinese_evidence() -> None:
    binding = _bind("Polyester POY reference price moved higher on Sep 11.", CN_SUMMARY)
    assert binding.supported is True
    assert binding.doc_ids == (CN_SUMMARY.doc_id,)


def test_wrong_year_date_must_not_be_supported() -> None:
    # Codex 复核 P1：证据 2026 年、论断写 2025 年——年份不同必须拒绝。
    binding = _bind("2025年9月11日 国家发改委对成品油价格实施调控，因国际油价持续大幅上涨。", RagEvidence(
        doc_id="news_event:evt_ndrc_price_2026",
        doc_type="news_event_cluster",
        source_id="nea_news",
        tier="A",
        title="2026年9月11日 发改委对成品油价格实施调控",
        summary="因国际油价持续大幅上涨，发改委启动价格调控。",
        observed_at="2026-09-11",
        visible_at="2026-09-11",
    ))
    assert binding.supported is False


def test_wrong_year_still_rejected_after_dedup() -> None:
    # Codex 复核 P1 回归：错年（2025 vs 2026）必须拒绝——无年变体不得绕过年份门禁。
    binding = _bind("2025年9月11日 国家发改委对成品油价格实施调控，因国际油价持续大幅上涨。", RagEvidence(
        doc_id="news_event:evt_ndrc_price_2026",
        doc_type="news_event_cluster",
        source_id="nea_news",
        tier="A",
        title="2026年9月11日 发改委对成品油价格实施调控",
        summary="因国际油价持续大幅上涨，发改委启动价格调控。",
        observed_at="2026-09-11",
        visible_at="2026-09-11",
    ))
    assert binding.supported is False


def test_year_omitted_in_claim_matches_full_date_in_evidence() -> None:
    # 论断省略年份（9月11日）对照完整日期（2026-09-11）：同月同日应算支持。
    binding = _bind("9月11日 涤纶POY参考价上行。", RagEvidence(
        doc_id="news_event:evt_poy_sep11",
        doc_type="news_event_cluster",
        source_id="texnet_polyester_news",
        tier="B",
        title="9月11日涤纶POY参考价上行",
        summary="涤纶POY参考价上行，下游按需采购。",
        observed_at="2026-09-11",
        visible_at="2026-09-11",
    ))
    assert binding.supported is True


def test_date_format_difference_no_longer_falsely_flags() -> None:
    # 论断日期 2026-09-11 与证据 9月11日 是同一天（C07 实测误伤场景）。
    binding = _bind("2026-09-11 国家发改委对成品油价格实施调控，因国际油价持续大幅上涨。", RagEvidence(
        doc_id="news_event:evt_ndrc_price",
        doc_type="news_event_cluster",
        source_id="nea_news",
        tier="A",
        title="国家发改委对成品油价格实施调控",
        summary="因国际油价持续大幅上涨，发改委启动价格调控。",
        observed_at="2026-09-11",
        visible_at="2026-09-11",
    ))
    assert binding.supported is True


def test_claim_date_missing_from_evidence_still_fails() -> None:
    binding = _bind("2026-09-11 国家发改委对成品油价格实施调控。", RagEvidence(
        doc_id="news_event:evt_unrelated_date",
        doc_type="news_event_cluster",
        source_id="test",
        tier="A",
        title="发改委召开年度工作会议",
        summary="会议总结全年工作并部署明年任务。",
        observed_at="2026-09-01",
        visible_at="2026-09-01",
    ))
    assert binding.supported is False


def test_english_month_date_matches_chinese_date() -> None:
    binding = _bind("EIA on Sep 10 forecast record U.S. crude production for 2026.", RagEvidence(
        doc_id="news_event:evt_eia_sep10",
        doc_type="news_event_cluster",
        source_id="eia_press",
        tier="A",
        title="EIA: 美国原油产量 2026-09-10 预测上调",
        summary="9月10日 EIA 上调美国原油产量预测。",
        observed_at="2026-09-10",
        visible_at="2026-09-10",
    ))
    assert binding.supported is True


def test_entity_mismatch_still_fails_closed() -> None:
    # 实体不同的相近句式不得因跨语言归一而误判支持。
    iran_evidence = RagEvidence(
        doc_id="news_event:evt_iran_output",
        doc_type="news_event_cluster",
        source_id="test",
        tier="A",
        title="Iran increases crude production forecast to a record",
        summary="Iranian output outlook raised.",
        observed_at="2026-09-11",
        visible_at="2026-09-11",
    )
    binding = _bind("EIA预测美国原油产量创纪录。", iran_evidence)
    # 证据主体是 Iran 而非 EIA/美国：实体门禁必须保持拒绝。
    assert binding.supported is False


def test_number_mismatch_still_fails_closed() -> None:
    binding = _bind("EIA预测美国原油产量达到108.5万桶/日。", RagEvidence(
        doc_id="news_event:evt_eia_no_number",
        doc_type="news_event_cluster",
        source_id="eia_press",
        tier="A",
        title="EIA increases U.S. crude production outlook",
        summary="Crude production outlook raised without a published figure.",
        observed_at="2026-09-11",
        visible_at="2026-09-11",
    ))
    # 论断带具体数字而证据无该数字：数字门禁保持拒绝。
    assert binding.supported is False


def test_same_language_terms_do_not_receive_duplicate_bilingual_credit() -> None:
    from app.citation import _cross_language_overlap
    assert _cross_language_overlap('POY价格上涨', 'POY价格上涨') == set()
    result = _bind('POY价格上涨', RagEvidence(doc_id='p', doc_type='news_article', source_id='test',
        tier='B', title='POY价格上涨', summary='POY价格上涨'))
    assert result.supported
    assert 0 <= result.score <= 1


def test_rfc_publication_date_and_known_reference_id_do_not_become_fact_numbers() -> None:
    evidence = RagEvidence(
        doc_id="news_article:art_0a2aabbd7f6747b4", doc_type="news_article", source_id="eia", tier="A",
        title="Eight petroleum liquids pipeline projects have been completed since the start of 2025",
        summary="自2025年初以来完成的液体燃料管道项目数量为8个，新宣布的项目数量为14个。",
        observed_at="Wed, 26 Aug 2026 09:00:00 EST",
    )
    claim = ("证据：news_article:art_0a2aabbd7f6747b4 显示：自2025年初以来完成的液体燃料管道项目"
             "数量为8个，新宣布的项目数量为14个，报道日期为2026年8月26日。")
    assert _bind(claim, evidence).supported
    assert not _bind(claim.replace("2026年8月26日", "2025年8月26日"), evidence).supported
    assert not _bind(claim.replace("14个", "99个"), evidence).supported
    assert not _bind(claim.replace("art_0a2aabbd7f6747b4", "art_999999"), evidence).supported
