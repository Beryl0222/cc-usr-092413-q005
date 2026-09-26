"""认证范围拆分与承接的领域合同测试。"""

import unittest

from certification import (
    CertificationError,
    CertificationService,
    CERT_ACTIVE,
    CERT_FROZEN,
    CERT_PARTIAL,
    CERT_SUSPENDED,
    CERT_REVOKED,
    CERT_SUPERSEDED,
    OUTCOME_INHERITED,
    OUTCOME_PARTIAL,
    OUTCOME_SUSPENDED,
    OUTCOME_REJECTED,
    CONFIRM_PASS,
    CONFIRM_REJECT,
)

VENUE = "subject-venue"
ONLINE = "subject-online"
OLD_PARTY = "原连锁康养机构"


def make_service():
    """构造含三个组件的典型原证书：旅居场地、线上签约、健康支持。"""
    service = CertificationService(clock=lambda: "2026-09-10")
    cert = service.register_certificate({
        "certificate_id": "cert-base",
        "holder": "连锁康养机构",
        "responsible_party": OLD_PARTY,
        "valid_until": "2027-09-09",
        "components": [
            {"id": "stay", "name": "旅居住宿"},
            {"id": "contract", "name": "线上签约"},
            {"id": "health", "name": "健康支持"},
        ],
        "population_tiers": [{"id": "self_care", "name": "自理"},
                             {"id": "assisted", "name": "介助"}],
        "locations": ["三亚基地", "线上"],
    })
    return service


class FreezeTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_freeze_captures_immutable_snapshot(self):
        frozen = self.service.freeze_certificate({"certificate_id": "cert-base"})
        self.assertEqual(frozen["status"], CERT_FROZEN)
        self.assertTrue(frozen["frozen"])
        snapshot = frozen["facts"][0]["detail"]["snapshot"]
        self.assertEqual(snapshot["responsible_party"], OLD_PARTY)
        self.assertEqual({c["id"] for c in snapshot["components"]},
                         {"stay", "contract", "health"})
        self.assertEqual(snapshot["valid_until"], "2027-09-09")

    def test_freeze_is_idempotent(self):
        first = self.service.freeze_certificate({"certificate_id": "cert-base"})
        second = self.service.freeze_certificate({"certificate_id": "cert-base"})
        self.assertEqual(len(first["facts"]), 1)
        self.assertEqual(len(second["facts"]), 1)

    def test_split_requires_freeze(self):
        with self.assertRaises(CertificationError):
            self.service.initiate_split({
                "certificate_id": "cert-base",
                "venue_subject_id": VENUE,
                "online_subject_id": ONLINE,
            })


def prepare_split(service, venue_components=("stay",), online_components=("contract", "health"),
                  venue_gaps=(), online_gaps=()):
    """冻结、发起拆分并由两个主体提交声明。"""
    service.freeze_certificate({"certificate_id": "cert-base"})
    split = service.initiate_split({
        "certificate_id": "cert-base",
        "venue_subject_id": VENUE,
        "online_subject_id": ONLINE,
    })
    service.submit_declaration({
        "split_id": split["split_id"], "subject_id": VENUE,
        "component_ids": list(venue_components),
        "controls": ["场地消防巡检", "持证护理员在岗"],
        "gaps": list(venue_gaps),
        "liability_accepted": True, "complaint_handling_accepted": True,
    })
    service.submit_declaration({
        "split_id": split["split_id"], "subject_id": ONLINE,
        "component_ids": list(online_components),
        "controls": ["电子签约留痕", "健康档案加密"],
        "gaps": list(online_gaps),
        "liability_accepted": True, "complaint_handling_accepted": True,
    })
    return split["split_id"]


class InheritanceCandidateTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_applicable_evidence_is_inheritable(self):
        split_id = prepare_split(self.service)
        self.service.add_evidence({
            "evidence_id": "ev-stay", "certificate_id": "cert-base",
            "component_id": "stay", "summary": "三亚基地入住体验观察",
            "population_tiers": ["self_care", "assisted"], "locations": ["三亚基地"],
        })
        candidates = self.service.inheritance_candidates(split_id)
        stay = candidates[VENUE]["stay"]
        self.assertEqual(stay["inheritable_evidence"], ["ev-stay"])
        self.assertEqual(stay["non_inherited"], [])

    def test_partial_evidence_records_reason(self):
        split_id = prepare_split(self.service)
        self.service.add_evidence({
            "evidence_id": "ev-contract", "certificate_id": "cert-base",
            "component_id": "contract", "summary": "仅自理人群完成线上签约回访",
            "applicability": "partial",
            "population_tiers": ["self_care"], "locations": ["线上"],
            "out_of_scope_reasons": ["介助人群无线上签约样本"],
        })
        candidates = self.service.inheritance_candidates(split_id)
        contract = candidates[ONLINE]["contract"]
        self.assertEqual(contract["inheritable_evidence"], [])
        reasons = contract["non_inherited"][0]["reasons"]
        self.assertIn("介助人群无线上签约样本", reasons)

    def test_missing_location_blocks_inheritance(self):
        split_id = prepare_split(self.service)
        self.service.add_evidence({
            "evidence_id": "ev-health", "certificate_id": "cert-base",
            "component_id": "health", "summary": "旧基地健康随访",
            "locations": ["已剥离的旧基地"],
        })
        candidates = self.service.inheritance_candidates(split_id)
        health = candidates[ONLINE]["health"]
        self.assertEqual(health["inheritable_evidence"], [])
        self.assertTrue(any("地点缺失" in r for r in health["non_inherited"][0]["reasons"]))

    def test_declared_gap_leads_to_partial_approval(self):
        split_id = prepare_split(
            self.service,
            venue_gaps=[{"component_id": "stay", "detail": "夜班护理员缺口"}],
        )
        self.service.add_evidence({
            "evidence_id": "ev-stay", "certificate_id": "cert-base",
            "component_id": "stay", "summary": "入住体验",
        })
        # 证据按适用范围仍可继承；自认缺口单列，不篡改证据适用性
        candidates = self.service.inheritance_candidates(split_id)
        self.assertEqual(candidates[VENUE]["stay"]["inheritable_evidence"], ["ev-stay"])
        for subject in (VENUE, ONLINE):
            for track in ("experience", "compliance"):
                self.service.confirm_review({
                    "split_id": split_id, "subject_id": subject,
                    "track": track, "decision": CONFIRM_PASS, "reviewer": "r",
                })
        result = self.service.issue({"split_id": split_id})
        venue_cert = next(c for c in result["issued"] if c["responsible_party"] == VENUE)
        stay_result = venue_cert["component_results"]["stay"]
        self.assertEqual(stay_result["outcome"], OUTCOME_PARTIAL)
        self.assertEqual(stay_result["inherited_evidence"], ["ev-stay"])
        self.assertTrue(stay_result["open_gaps"])


class ReviewGateTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.split_id = prepare_split(self.service)

    def confirm(self, subject, track, decision=CONFIRM_PASS):
        self.service.confirm_review({
            "split_id": self.split_id, "subject_id": subject,
            "track": track, "decision": decision,
            "reviewer": f"r-{track}",
        })

    def test_issue_requires_both_tracks_for_each_subject(self):
        self.confirm(VENUE, "experience")
        with self.assertRaises(CertificationError):
            self.service.issue({"split_id": self.split_id})

    def test_compliance_cannot_pass_without_liability_and_complaint_acceptance(self):
        service = make_service()
        service.freeze_certificate({"certificate_id": "cert-base"})
        split = service.initiate_split({
            "certificate_id": "cert-base",
            "venue_subject_id": VENUE, "online_subject_id": ONLINE,
        })
        service.submit_declaration({
            "split_id": split["split_id"], "subject_id": VENUE,
            "component_ids": ["stay"], "controls": ["巡检"],
            "gaps": [], "liability_accepted": False,
            "complaint_handling_accepted": False,
        })
        service.submit_declaration({
            "split_id": split["split_id"], "subject_id": ONLINE,
            "component_ids": ["contract", "health"], "controls": ["加密"],
            "liability_accepted": True, "complaint_handling_accepted": True,
        })
        with self.assertRaises(CertificationError):
            service.confirm_review({
                "split_id": split["split_id"], "subject_id": VENUE,
                "track": "compliance", "decision": CONFIRM_PASS, "reviewer": "legal",
            })


class IssueTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.split_id = prepare_split(self.service)
        for component, summary in [
            ("stay", "入住体验"), ("contract", "签约体验"), ("health", "健康随访"),
        ]:
            self.service.add_evidence({
                "evidence_id": f"ev-{component}", "certificate_id": "cert-base",
                "component_id": component, "summary": summary,
                "population_tiers": ["self_care", "assisted"],
                "locations": ["三亚基地", "线上"],
            })

    def confirm_all(self, venue_decision=CONFIRM_PASS, online_decision=CONFIRM_PASS):
        for subject, decision in ((VENUE, venue_decision), (ONLINE, online_decision)):
            for track in ("experience", "compliance"):
                self.service.confirm_review({
                    "split_id": self.split_id, "subject_id": subject,
                    "track": track, "decision": decision, "reviewer": f"r-{track}",
                })

    def test_independent_versions_issued(self):
        self.confirm_all()
        result = self.service.issue({"split_id": self.split_id})
        self.assertFalse(result["replayed"])
        issued = result["issued"]
        self.assertEqual(len(issued), 2)
        venue_cert = next(c for c in issued if c["responsible_party"] == VENUE)
        online_cert = next(c for c in issued if c["responsible_party"] == ONLINE)
        self.assertEqual(venue_cert["scope"]["components"], ["stay"])
        self.assertEqual(set(online_cert["scope"]["components"]), {"contract", "health"})
        self.assertEqual(venue_cert["version"], 1)
        self.assertEqual(online_cert["version"], 1)
        self.assertNotEqual(venue_cert["certificate_id"], online_cert["certificate_id"])
        self.assertEqual(venue_cert["generated_from"]["original_certificate_id"], "cert-base")

    def test_overlapping_component_ownership_rejected(self):
        service = make_service()
        split_id = prepare_split(
            service,
            venue_components=("stay", "contract"),
            online_components=("contract", "health"),
        )
        for subject in (VENUE, ONLINE):
            for track in ("experience", "compliance"):
                service.confirm_review({
                    "split_id": split_id, "subject_id": subject,
                    "track": track, "decision": CONFIRM_PASS, "reviewer": "r",
                })
        with self.assertRaises(CertificationError):
            service.issue({"split_id": split_id})

    def test_replay_does_not_reissue(self):
        self.confirm_all()
        first = self.service.issue({"split_id": self.split_id})
        second = self.service.issue({"split_id": self.split_id})
        self.assertTrue(second["replayed"])
        first_ids = {c["certificate_id"] for c in first["issued"]}
        second_ids = {c["certificate_id"] for c in second["issued"]}
        self.assertEqual(first_ids, second_ids)


class SuspensionTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.split_id = prepare_split(self.service)
        for component in ("stay", "contract", "health"):
            self.service.add_evidence({
                "evidence_id": f"ev-{component}", "certificate_id": "cert-base",
                "component_id": component, "summary": "体验",
                "population_tiers": ["self_care", "assisted"],
                "locations": ["三亚基地", "线上"],
            })

    def confirm_and_issue(self):
        for subject in (VENUE, ONLINE):
            for track in ("experience", "compliance"):
                self.service.confirm_review({
                    "split_id": self.split_id, "subject_id": subject,
                    "track": track, "decision": CONFIRM_PASS, "reviewer": "r",
                })
        return self.service.issue({"split_id": self.split_id})

    def test_inseverable_event_suspends_only_related_component(self):
        self.service.add_severe_event({
            "event_id": "evt-fall", "certificate_id": "cert-base",
            "kind": "severe_event", "summary": "健康支持严重不良事件未结案",
            "component_ids": ["health"], "severable": False,
        })
        result = self.confirm_and_issue()
        online_cert = next(c for c in result["issued"] if c["responsible_party"] == ONLINE)
        # health 暂停，但无关的 contract 仍然通过，不被拖累
        self.assertEqual(online_cert["status"], CERT_PARTIAL)
        self.assertEqual(online_cert["scope"]["components"], ["contract"])
        self.assertEqual(online_cert["suspended_components"], ["health"])
        self.assertEqual(
            online_cert["component_results"]["health"]["outcome"], OUTCOME_SUSPENDED
        )
        self.assertEqual(
            online_cert["component_results"]["contract"]["outcome"], OUTCOME_INHERITED
        )
        venue_cert = next(c for c in result["issued"] if c["responsible_party"] == VENUE)
        self.assertEqual(venue_cert["status"], CERT_ACTIVE)

    def test_shared_facility_without_scope_hits_all_components(self):
        self.service.add_severe_event({
            "event_id": "evt-shared", "certificate_id": "cert-base",
            "kind": "shared_facility", "summary": "场地产权共用未切割",
        })
        result = self.confirm_and_issue()
        venue_cert = next(c for c in result["issued"] if c["responsible_party"] == VENUE)
        self.assertEqual(venue_cert["status"], CERT_SUSPENDED)
        self.assertEqual(venue_cert["scope"]["components"], [])

    def test_severable_event_does_not_block(self):
        self.service.add_severe_event({
            "event_id": "evt-ok", "certificate_id": "cert-base",
            "kind": "shared_staff", "summary": "排班已切割的兼职人员",
            "component_ids": ["stay"], "severable": True,
        })
        result = self.confirm_and_issue()
        venue_cert = next(c for c in result["issued"] if c["responsible_party"] == VENUE)
        self.assertEqual(venue_cert["status"], CERT_ACTIVE)

    def test_remediation_resolves_event_and_reissue_restores_scope(self):
        self.service.add_severe_event({
            "event_id": "evt-fall", "certificate_id": "cert-base",
            "kind": "severe_event", "summary": "不良事件",
            "component_ids": ["health"], "severable": False,
        })
        first = self.confirm_and_issue()
        online_v1 = next(c for c in first["issued"] if c["responsible_party"] == ONLINE)
        self.service.submit_remediation({
            "certificate_id": online_v1["certificate_id"],
            "component_id": "health",
            "summary": "完成事件整改并提交纠正措施",
            "resolves_events": ["evt-fall"],
            "submitted_by": ONLINE,
        })
        second = self.service.issue({"split_id": self.split_id})
        online_v2 = next(c for c in second["issued"] if c["responsible_party"] == ONLINE)
        self.assertIn("health", online_v2["scope"]["components"])
        self.assertEqual(online_v2["status"], CERT_ACTIVE)
        self.assertEqual(online_v2["version"], 2)
        # 旧版本被取代，整改事实以追加方式保留
        self.assertEqual(
            self.service.public_certificate(online_v1["certificate_id"])["status"],
            CERT_SUPERSEDED,
        )
        facts = self.service.public_certificate(online_v1["certificate_id"])["facts"]
        self.assertTrue(any(f["type"] == "remediation" for f in facts))
        self.assertTrue(any(
            f["detail"].get("superseded_by") == online_v2["certificate_id"] for f in facts
        ))
        # 从新版本追溯时，挂在旧版本上的整改事实仍可在版本链中查到
        trace = self.service.trace(online_v2["certificate_id"])
        history_facts = [f for v in trace["version_history"] for f in v["facts"]]
        self.assertTrue(any(f["type"] == "remediation" for f in history_facts))


class PartialAndRejectTest(unittest.TestCase):
    def test_partial_approval_when_some_evidence_missing(self):
        service = make_service()
        split_id = prepare_split(service)
        # contract 有完整证据，health 无证据
        service.add_evidence({
            "evidence_id": "ev-contract", "certificate_id": "cert-base",
            "component_id": "contract", "summary": "签约体验",
            "population_tiers": ["self_care", "assisted"], "locations": ["线上"],
        })
        for subject in (VENUE, ONLINE):
            for track in ("experience", "compliance"):
                service.confirm_review({
                    "split_id": split_id, "subject_id": subject,
                    "track": track, "decision": CONFIRM_PASS, "reviewer": "r",
                })
        # venue 的 stay 也无证据：venue 全部驳回；online 部分通过
        service.add_evidence({
            "evidence_id": "ev-stay", "certificate_id": "cert-base",
            "component_id": "stay", "summary": "入住体验",
        })
        result = service.issue({"split_id": split_id})
        venue_cert = next(c for c in result["issued"] if c["responsible_party"] == VENUE)
        online_cert = next(c for c in result["issued"] if c["responsible_party"] == ONLINE)
        self.assertEqual(
            venue_cert["component_results"]["stay"]["outcome"], OUTCOME_INHERITED
        )
        self.assertEqual(
            online_cert["component_results"]["health"]["outcome"], OUTCOME_REJECTED
        )
        self.assertEqual(online_cert["scope"]["components"], ["contract"])
        self.assertEqual(online_cert["rejected_components"], ["health"])

    def test_rejected_review_leaves_scope_out(self):
        service = make_service()
        split_id = prepare_split(service)
        service.add_evidence({
            "evidence_id": "ev-stay", "certificate_id": "cert-base",
            "component_id": "stay", "summary": "入住体验",
        })
        # venue 体验被驳回
        service.confirm_review({
            "split_id": split_id, "subject_id": VENUE,
            "track": "experience", "decision": CONFIRM_REJECT, "reviewer": "r1",
        })
        service.confirm_review({
            "split_id": split_id, "subject_id": VENUE,
            "track": "compliance", "decision": CONFIRM_PASS, "reviewer": "r2",
        })
        for track in ("experience", "compliance"):
            service.confirm_review({
                "split_id": split_id, "subject_id": ONLINE,
                "track": track, "decision": CONFIRM_PASS, "reviewer": "r",
            })
        result = service.issue({"split_id": split_id})
        venue_cert = next(c for c in result["issued"] if c["responsible_party"] == VENUE)
        self.assertEqual(
            venue_cert["component_results"]["stay"]["outcome"], OUTCOME_REJECTED
        )
        self.assertEqual(venue_cert["status"], CERT_REVOKED)


class ContentChangeReviewTest(unittest.TestCase):
    def test_changed_declaration_invalidates_review_and_versions(self):
        service = make_service()
        split_id = prepare_split(service)
        service.add_evidence({
            "evidence_id": "ev-stay", "certificate_id": "cert-base",
            "component_id": "stay", "summary": "入住体验",
        })
        for subject in (VENUE, ONLINE):
            for track in ("experience", "compliance"):
                service.confirm_review({
                    "split_id": split_id, "subject_id": subject,
                    "track": track, "decision": CONFIRM_PASS, "reviewer": "r",
                })
        first = service.issue({"split_id": split_id})
        v1_id = first["issued"][0]["certificate_id"]
        # 承接内容变化：增加新控制措施
        service.submit_declaration({
            "split_id": split_id, "subject_id": VENUE,
            "component_ids": ["stay"],
            "controls": ["场地消防巡检", "持证护理员在岗", "新增夜间双人巡查"],
            "liability_accepted": True, "complaint_handling_accepted": True,
        })
        with self.assertRaises(CertificationError):
            service.issue({"split_id": split_id})
        for track in ("experience", "compliance"):
            service.confirm_review({
                "split_id": split_id, "subject_id": VENUE,
                "track": track, "decision": CONFIRM_PASS, "reviewer": "r2",
            })
        second = service.issue({"split_id": split_id})
        self.assertFalse(second["replayed"])
        v2 = next(c for c in second["issued"] if c["responsible_party"] == VENUE)
        self.assertEqual(v2["version"], 2)
        self.assertEqual(
            service.public_certificate(v1_id)["status"], CERT_SUPERSEDED
        )


class CitationTest(unittest.TestCase):
    def test_citation_must_point_to_explicit_current_version(self):
        service = make_service()
        split_id = prepare_split(service)
        service.add_evidence({
            "evidence_id": "ev-stay", "certificate_id": "cert-base",
            "component_id": "stay", "summary": "入住体验",
        })
        for subject in (VENUE, ONLINE):
            for track in ("experience", "compliance"):
                service.confirm_review({
                    "split_id": split_id, "subject_id": subject,
                    "track": track, "decision": CONFIRM_PASS, "reviewer": "r",
                })
        result = service.issue({"split_id": split_id})
        venue_cert = next(c for c in result["issued"] if c["responsible_party"] == VENUE)
        record = service.register_citation({
            "certificate_id": venue_cert["certificate_id"],
            "citation": "官网认证标识", "registered_by": "market",
        })
        self.assertEqual(record["version"], 1)
        with self.assertRaises(CertificationError):
            service.register_citation({
                "certificate_id": venue_cert["certificate_id"], "version": 99,
                "citation": "旧版宣传", "registered_by": "market",
            })
        # 冻结中的原证书不允许引用
        with self.assertRaises(CertificationError):
            service.register_citation({
                "certificate_id": "cert-base",
                "citation": "旧宣传", "registered_by": "market",
            })

    def test_superseded_version_cannot_be_cited(self):
        service = make_service()
        split_id = prepare_split(service)
        service.add_evidence({
            "evidence_id": "ev-stay", "certificate_id": "cert-base",
            "component_id": "stay", "summary": "入住体验",
        })
        for subject in (VENUE, ONLINE):
            for track in ("experience", "compliance"):
                service.confirm_review({
                    "split_id": split_id, "subject_id": subject,
                    "track": track, "decision": CONFIRM_PASS, "reviewer": "r",
                })
        first = service.issue({"split_id": split_id})
        v1_id = first["issued"][0]["certificate_id"]
        service.submit_declaration({
            "split_id": split_id, "subject_id": VENUE,
            "component_ids": ["stay"], "controls": ["新措施"],
            "liability_accepted": True, "complaint_handling_accepted": True,
        })
        for track in ("experience", "compliance"):
            service.confirm_review({
                "split_id": split_id, "subject_id": VENUE,
                "track": track, "decision": CONFIRM_PASS, "reviewer": "r2",
            })
        service.issue({"split_id": split_id})
        with self.assertRaises(CertificationError):
            service.register_citation({
                "certificate_id": v1_id,
                "citation": "旧版宣传", "registered_by": "market",
            })


class RevocationAndFactsTest(unittest.TestCase):
    def test_component_revocation_appends_fact_and_keeps_rest(self):
        service = make_service()
        split_id = prepare_split(
            service, venue_components=("stay",), online_components=("contract", "health"))
        for component in ("stay", "contract", "health"):
            service.add_evidence({
                "evidence_id": f"ev-{component}", "certificate_id": "cert-base",
                "component_id": component, "summary": "体验",
                "population_tiers": ["self_care", "assisted"],
                "locations": ["三亚基地", "线上"],
            })
        for subject in (VENUE, ONLINE):
            for track in ("experience", "compliance"):
                service.confirm_review({
                    "split_id": split_id, "subject_id": subject,
                    "track": track, "decision": CONFIRM_PASS, "reviewer": "r",
                })
        result = service.issue({"split_id": split_id})
        online_cert = next(c for c in result["issued"] if c["responsible_party"] == ONLINE)
        service.revoke_scope({
            "certificate_id": online_cert["certificate_id"],
            "component_id": "health", "reason": "健康支持复查不合格",
            "revoked_by": "board",
        })
        view = service.public_certificate(online_cert["certificate_id"])
        self.assertEqual(view["scope"]["components"], ["contract"])
        self.assertTrue(any(f["type"] == "revocation" for f in view["facts"]))


class ConsumerAndTraceTest(unittest.TestCase):
    def _issued_service(self, with_event=False):
        service = make_service()
        service.register_consumer({
            "consumer_id": "csm-stay", "certificate_id": "cert-base",
            "component_id": "stay", "commitment": "原订单旅居权益至2027年",
        })
        service.register_consumer({
            "consumer_id": "csm-health", "certificate_id": "cert-base",
            "component_id": "health", "commitment": "原健康随访套餐",
        })
        split_id = prepare_split(service)
        for component in ("stay", "contract", "health"):
            service.add_evidence({
                "evidence_id": f"ev-{component}", "certificate_id": "cert-base",
                "component_id": component, "summary": "体验",
                "population_tiers": ["self_care", "assisted"],
                "locations": ["三亚基地", "线上"],
            })
        if with_event:
            service.add_severe_event({
                "event_id": "evt-fall", "certificate_id": "cert-base",
                "kind": "severe_event", "summary": "健康事件",
                "component_ids": ["health"], "severable": False,
            })
        for subject in (VENUE, ONLINE):
            for track in ("experience", "compliance"):
                service.confirm_review({
                    "split_id": split_id, "subject_id": subject,
                    "track": track, "decision": CONFIRM_PASS, "reviewer": "r",
                })
        result = service.issue({"split_id": split_id})
        return service, result

    def test_consumers_keep_commitment_and_get_responsibility_record(self):
        service, result = self._issued_service()
        venue_cert = next(c for c in result["issued"] if c["responsible_party"] == VENUE)
        stay = service.consumers["csm-stay"]
        self.assertEqual(stay["status"], "commitment_retained")
        self.assertEqual(stay["commitment"], "原订单旅居权益至2027年")
        self.assertEqual(stay["responsibility_history"][0]["to_subject"], VENUE)
        self.assertEqual(
            stay["responsibility_history"][0]["new_certificate_id"],
            venue_cert["certificate_id"],
        )
        self.assertTrue(stay["notices"])

    def test_suspended_scope_consumer_retained_without_transfer(self):
        service, result = self._issued_service(with_event=True)
        health = service.consumers["csm-health"]
        self.assertEqual(health["status"], "service_suspended_retained")
        self.assertTrue(any(
            n["type"] == "scope_suspended" and n["blocking_events"] == ["evt-fall"]
            for n in health["notices"]
        ))

    def test_trace_from_new_cert_to_evidence_reasons_and_consumers(self):
        service, result = self._issued_service()
        venue_cert = next(c for c in result["issued"] if c["responsible_party"] == VENUE)
        trace = service.trace(venue_cert["certificate_id"])
        self.assertEqual(trace["original_snapshot"]["responsible_party"], OLD_PARTY)
        stay_trace = next(c for c in trace["components"] if c["component_id"] == "stay")
        self.assertEqual(stay_trace["inherited_evidence"], ["ev-stay"])
        self.assertEqual(stay_trace["source_evidence"][0]["summary"], "体验")
        consumer_ids = {c["consumer_id"] for c in trace["consumer_dispositions"]}
        self.assertIn("csm-stay", consumer_ids)
        self.assertNotIn("csm-health", consumer_ids)
        # 暂停组件在另一张新证书上仍可追溯消费者处置
        service2, result2 = self._issued_service(with_event=True)
        online_cert = next(c for c in result2["issued"] if c["responsible_party"] == ONLINE)
        trace2 = service2.trace(online_cert["certificate_id"])
        health_trace = next(c for c in trace2["components"] if c["component_id"] == "health")
        self.assertEqual(health_trace["outcome"], OUTCOME_SUSPENDED)
        self.assertEqual(health_trace["blocking_events"], ["evt-fall"])
        dispositions = {c["consumer_id"]: c for c in trace2["consumer_dispositions"]}
        self.assertEqual(
            dispositions["csm-health"]["status"], "service_suspended_retained"
        )


if __name__ == "__main__":
    unittest.main()
