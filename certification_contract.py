"""认证范围拆分与承接能力的领域契约测试。

以“康养连锁旅居服务拆分为场地承接公司与线上健康公司”为主场景，逐条钉住：
冻结快照、证据适用范围候选、双确认、阻断隔离、独立新版本、部分通过、
整改与复审、幂等重放、版本钉住宣传、消费者承诺保留与责任变更、双向溯源。
"""

import unittest

from certification import CertificationService, DomainError


class SplitFixture:
    """构造康养机构拆分场景的标准夹具。"""

    COMPONENTS = [
        {"id": "stay", "name": "旅居住宿",
         "populations": ["self-care", "assisted"], "locations": ["beijing", "sanya"],
         "responsible_party_org_id": "org-original"},
        {"id": "signing", "name": "线上签约",
         "populations": ["self-care", "assisted"], "locations": ["online"],
         "responsible_party_org_id": "org-original"},
        {"id": "health", "name": "健康支持",
         "populations": ["self-care", "assisted"], "locations": ["online", "beijing"],
         "responsible_party_org_id": "org-original"},
    ]

    def __init__(self):
        self.svc = CertificationService()
        for oid, name in [
            ("org-original", "原康养连锁"),
            ("org-venue", "场地承接公司"),
            ("org-online", "线上健康公司"),
        ]:
            self.cmd("register_organization", {"org_id": oid, "name": name})
        self.cmd(
            "register_certificate",
            {
                "cert_id": "cert-1",
                "holder_org_id": "org-original",
                "valid_from": "2025-01-01",
                "valid_to": "2027-01-01",
                "components": self.COMPONENTS,
            },
        )
        for evidence in [
            ("ev-stay", "stay", ["self-care", "assisted"], ["beijing", "sanya"]),
            ("ev-sign", "signing", ["self-care", "assisted"], ["online"]),
            ("ev-health-online", "health", ["self-care"], ["online"]),
            ("ev-health-beijing", "health", ["assisted"], ["beijing"]),
        ]:
            self.cmd(
                "record_evidence",
                {
                    "evidence_id": evidence[0],
                    "cert_id": "cert-1",
                    "component_id": evidence[1],
                    "populations": evidence[2],
                    "locations": evidence[3],
                    "summary": f"证据 {evidence[0]}",
                },
            )

    def cmd(self, name, params=None, key=None):
        return self.svc.command(name, params, idempotency_key=key)

    def open_split(self, split_id="split-1"):
        return self.cmd("open_split", {"split_id": split_id, "cert_id": "cert-1"})

    def declare_both(self):
        venue = self.cmd(
            "declare_succession",
            {
                "split_id": "split-1",
                "successor_org_id": "org-venue",
                "items": [
                    {"component_id": "stay", "controls": ["驻场巡检"], "gaps": [],
                     "staff_ids": ["staff-li"], "facility_ids": ["sanya"]}
                ],
            },
        )
        online = self.cmd(
            "declare_succession",
            {
                "split_id": "split-1",
                "successor_org_id": "org-online",
                "items": [
                    {"component_id": "signing", "controls": ["电子签"], "gaps": [],
                     "staff_ids": [], "facility_ids": []},
                    {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
                     "controls": ["远程问诊"], "gaps": [], "staff_ids": [], "facility_ids": []},
                ],
            },
        )
        return venue, online

    def confirm(self, org_id, items, reviewer="reviewer-1", officer="officer-1"):
        self.cmd(
            "confirm_experience_coverage",
            {
                "split_id": "split-1",
                "successor_org_id": org_id,
                "reviewer": reviewer,
                "decisions": [
                    {"component_id": cid, "accepted_evidence_ids": ev, "resolved_gaps": gaps}
                    for cid, ev, gaps in items
                ],
            },
        )
        self.cmd(
            "confirm_responsibility_handover",
            {
                "split_id": "split-1",
                "successor_org_id": org_id,
                "compliance_officer": officer,
                "decisions": [
                    {"component_id": cid, "complaint_channel": "hotline-1", "liability_acknowledged": True}
                    for cid, _ev, _gaps in items
                ],
            },
        )


class FreezeTest(unittest.TestCase):
    def setUp(self):
        self.fx = SplitFixture()

    def test_open_split_freezes_all_five_boundary_elements(self):
        opened = self.fx.open_split()
        self.assertEqual(opened["status"], "frozen")
        self.assertEqual(opened["frozen_components"], ["stay", "signing", "health"])
        split = self.fx.svc.get_split("split-1")
        frozen = split["frozen"]
        # 服务组件、人群分层、地点、责任方、有效期全部冻结
        self.assertEqual(
            {c["id"]: {
                "populations": [s["id"] for s in c["populations"]],
                "locations": [s["id"] for s in c["locations"]],
                "responsible_party_org_id": c["responsible_party_org_id"],
            } for c in frozen["components"]},
            {
                "stay": {"populations": ["self-care", "assisted"], "locations": ["beijing", "sanya"],
                         "responsible_party_org_id": "org-original"},
                "signing": {"populations": ["self-care", "assisted"], "locations": ["online"],
                            "responsible_party_org_id": "org-original"},
                "health": {"populations": ["self-care", "assisted"], "locations": ["online", "beijing"],
                           "responsible_party_org_id": "org-original"},
            },
        )
        self.assertEqual(frozen["valid_from"], "2025-01-01")
        self.assertEqual(frozen["valid_to"], "2027-01-01")
        self.assertEqual(frozen["holder_org_id"], "org-original")
        self.assertEqual(self.fx.svc.get_certificate("cert-1")["status"], "frozen")

    def test_frozen_boundary_cannot_be_extended(self):
        self.fx.open_split()
        with self.assertRaises(DomainError) as error:
            self.fx.cmd(
                "declare_succession",
                {
                    "split_id": "split-1",
                    "successor_org_id": "org-online",
                    "items": [{"component_id": "health", "populations": ["dementia-care"],
                               "locations": ["shanghai"], "controls": [], "gaps": []}],
                },
            )
        self.assertEqual(error.exception.code, "SCOPE_OUTSIDE_FROZEN_BOUNDARY")
        self.assertEqual(set(error.exception.details), {"populations", "locations"})

    def test_same_component_cannot_be_claimed_by_two_successors(self):
        self.fx.open_split()
        self.fx.declare_both()
        with self.assertRaises(DomainError) as error:
            self.fx.cmd(
                "declare_succession",
                {"split_id": "split-1", "successor_org_id": "org-venue",
                 "items": [{"component_id": "health", "controls": []}]},
            )
        self.assertEqual(error.exception.code, "DECLARATION_EXISTS")


class EvidenceCandidatesTest(unittest.TestCase):
    def setUp(self):
        self.fx = SplitFixture()
        self.fx.open_split()

    def test_candidates_follow_component_population_location_scope(self):
        _venue, online = self.fx.declare_both()
        health = online["evidence_candidates"]["health"]
        # self-care × online 有证据，可继承
        self.assertEqual(health["applicable"], ["ev-health-online"])
        # assisted × beijing 的证据对线上承接范围不适用，并给出人群和地点两类缺口原因
        non = {e["evidence_id"]: e for e in health["non_applicable"]}
        self.assertIn("ev-health-beijing", non)
        codes = {r["code"] for r in non["ev-health-beijing"]["reasons"]}
        self.assertEqual(codes, {"evidence_population_gap", "evidence_location_gap"})
        self.assertEqual(non["ev-health-beijing"]["reasons"][0]["segments"], ["self-care"])

    def test_signing_evidence_is_not_offered_for_health(self):
        _venue, online = self.fx.declare_both()
        self.assertNotIn("ev-sign", online["evidence_candidates"]["health"]["applicable"])


class ConfirmationGateTest(unittest.TestCase):
    def setUp(self):
        self.fx = SplitFixture()
        self.fx.open_split()
        self.fx.declare_both()

    def test_issue_requires_both_confirmations(self):
        with self.assertRaises(DomainError) as error:
            self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        self.assertEqual(error.exception.code, "CONFIRMATIONS_STALE")
        self.assertEqual(
            set(error.exception.details["stale_confirmations"]),
            {"experience_coverage", "responsibility_handover"},
        )

    def test_unresolved_gap_blocks_experience_confirmation(self):
        # 修订线上主体，重新声明一个未关闭缺口
        self.fx.cmd(
            "amend_succession_declaration",
            {
                "split_id": "split-1",
                "successor_org_id": "org-online",
                "items": [
                    {"component_id": "signing", "controls": ["电子签"], "gaps": []},
                    {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
                     "controls": ["远程问诊"], "gaps": ["night-shift"]},
                ],
            },
        )
        with self.assertRaises(DomainError) as error:
            self.fx.cmd(
                "confirm_experience_coverage",
                {"split_id": "split-1", "successor_org_id": "org-online", "reviewer": "r1",
                 "decisions": [
                     {"component_id": "signing", "accepted_evidence_ids": ["ev-sign"]},
                     {"component_id": "health", "accepted_evidence_ids": ["ev-health-online"]},
                 ]},
            )
        self.assertEqual(error.exception.code, "UNRESOLVED_GAPS")
        self.assertEqual(error.exception.details["gaps"], ["night-shift"])

    def test_rectification_fact_is_appended_and_gap_can_close(self):
        self.fx.cmd(
            "amend_succession_declaration",
            {
                "split_id": "split-1",
                "successor_org_id": "org-online",
                "items": [
                    {"component_id": "signing", "controls": ["电子签"], "gaps": []},
                    {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
                     "controls": ["远程问诊"], "gaps": ["night-shift"]},
                ],
            },
        )
        self.fx.cmd(
            "record_rectification",
            {"split_id": "split-1", "successor_org_id": "org-online", "rectification_id": "rect-1",
             "component_id": "health", "gap": "night-shift", "action": "上线夜间值班并演练",
             "status": "completed"},
        )
        # 修订声明移除缺口后确认通过；整改事实仍保留在拆分视图中
        self.fx.cmd(
            "amend_succession_declaration",
            {
                "split_id": "split-1",
                "successor_org_id": "org-online",
                "items": [
                    {"component_id": "signing", "controls": ["电子签"], "gaps": []},
                    {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
                     "controls": ["远程问诊", "夜间值班"], "gaps": []},
                ],
            },
        )
        self.fx.confirm("org-online", [("signing", ["ev-sign"], []), ("health", ["ev-health-online"], [])])
        split = self.fx.svc.get_split("split-1")
        online = next(s for s in split["successors"] if s["successor_org_id"] == "org-online")
        self.assertEqual([r["rectification_id"] for r in online["rectifications"]], ["rect-1"])

    def test_responsibility_requires_complaint_channel_and_liability(self):
        with self.assertRaises(DomainError) as error:
            self.fx.cmd(
                "confirm_responsibility_handover",
                {"split_id": "split-1", "successor_org_id": "org-online", "compliance_officer": "c1",
                 "decisions": [
                     {"component_id": "signing", "complaint_channel": "app", "liability_acknowledged": True},
                     {"component_id": "health", "liability_acknowledged": True},
                 ]},
            )
        self.assertEqual(error.exception.code, "MISSING_COMPLAINT_CHANNEL")

    def test_reviewer_cannot_inherit_non_applicable_evidence(self):
        with self.assertRaises(DomainError) as error:
            self.fx.cmd(
                "confirm_experience_coverage",
                {"split_id": "split-1", "successor_org_id": "org-online", "reviewer": "r1",
                 "decisions": [
                     {"component_id": "signing", "accepted_evidence_ids": ["ev-sign"]},
                     {"component_id": "health", "accepted_evidence_ids": ["ev-health-beijing"]},
                 ]},
            )
        self.assertEqual(error.exception.code, "EVIDENCE_NOT_APPLICABLE")
        self.assertEqual(error.exception.details["evidence_ids"], ["ev-health-beijing"])


class BlockerIsolationTest(unittest.TestCase):
    def setUp(self):
        self.fx = SplitFixture()
        self.fx.open_split()
        self.fx.declare_both()

    def test_inseparable_incident_suspends_only_related_scope(self):
        # 严重事件只涉及 stay：venue 的 stay 暂停；online 的 signing/health 不受拖累
        self.fx.cmd(
            "register_severe_incident",
            {"split_id": "split-1", "incident_id": "inc-1", "component_ids": ["stay"],
             "inseparable": True, "description": "场地摔伤责任无法在主体间分割"},
        )
        self.fx.confirm("org-online", [("signing", ["ev-sign"], []), ("health", ["ev-health-online"], [])])
        issued = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        self.assertEqual(issued["version"], 1)
        self.assertEqual(set(issued["included_components"]), {"signing", "health"})

        self.fx.confirm("org-venue", [("stay", ["ev-stay"], [])])
        with self.assertRaises(DomainError) as error:
            self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        self.assertEqual(error.exception.code, "NO_ELIGIBLE_SCOPE")
        suspended = error.exception.details["suspended"]
        self.assertEqual(suspended[0]["component_id"], "stay")
        self.assertTrue(suspended[0]["reasons"][0]["kind"].startswith("severe_incident"))

        # 暂停记录进入已签发的线上证书视图？不应出现——它与 online 无关
        online_cert = self.fx.svc.get_certificate(issued["cert_id"])
        self.assertEqual(online_cert["versions"][0]["suspended_components"], [])

    def test_closing_incident_allows_venue_to_issue(self):
        self.fx.cmd(
            "register_severe_incident",
            {"split_id": "split-1", "incident_id": "inc-1", "component_ids": ["stay"],
             "inseparable": True, "description": "x"},
        )
        self.fx.confirm("org-venue", [("stay", ["ev-stay"], [])])
        self.fx.cmd("close_severe_incident",
                    {"split_id": "split-1", "incident_id": "inc-1", "resolution": "责任协议公证"})
        issued = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        self.assertEqual(issued["included_components"], ["stay"])

    def test_shared_staff_suspends_only_components_using_that_staff(self):
        # venue 改用与 online 相同的 staff 服务 signing？构造一个只命中 stay+signing 的共用人员：
        # online 修订 signing 使用 staff-wang，venue 的 stay 也使用 staff-wang。
        self.fx.cmd(
            "amend_succession_declaration",
            {"split_id": "split-1", "successor_org_id": "org-venue",
             "items": [{"component_id": "stay", "controls": ["驻场巡检"], "gaps": [],
                        "staff_ids": ["staff-wang"], "facility_ids": ["sanya"]}]},
        )
        self.fx.cmd(
            "amend_succession_declaration",
            {"split_id": "split-1", "successor_org_id": "org-online",
             "items": [
                 {"component_id": "signing", "controls": ["电子签"], "gaps": [],
                  "staff_ids": ["staff-wang"], "facility_ids": []},
                 {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
                  "controls": ["远程问诊"], "gaps": [], "staff_ids": [], "facility_ids": []},
             ]},
        )
        detected = [f for f in self.fx.svc.facts() if f["type"] == "BlockerDetected"]
        self.assertTrue(any(f["payload"]["kind"] == "shared_staff"
                            and set(f["payload"]["component_ids"]) == {"signing", "stay"}
                            for f in detected), detected)
        self.fx.confirm("org-online", [("signing", ["ev-sign"], []), ("health", ["ev-health-online"], [])])
        issued = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        # signing 暂停，health 照常通过（部分通过）
        self.assertEqual(issued["included_components"], ["health"])
        self.assertEqual(issued["suspended_components"][0]["component_id"], "signing")
        self.assertEqual(issued["suspended_components"][0]["reasons"][0]["kind"], "shared_staff")

    def test_shared_facility_is_detected_as_blocker(self):
        self.fx.cmd(
            "amend_succession_declaration",
            {"split_id": "split-1", "successor_org_id": "org-online",
             "items": [
                 {"component_id": "signing", "controls": ["电子签"], "gaps": [],
                  "staff_ids": [], "facility_ids": ["sanya"]},
                 {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
                  "controls": ["远程问诊"], "gaps": [], "staff_ids": [], "facility_ids": []},
             ]},
        )
        split = self.fx.svc.get_split("split-1")
        self.assertIn("shared_facility", {b["kind"] for b in split["active_blockers"]})


class IssuanceTest(unittest.TestCase):
    def setUp(self):
        self.fx = SplitFixture()
        self.fx.open_split()
        self.fx.declare_both()
        self.fx.confirm("org-venue", [("stay", ["ev-stay"], [])])
        self.fx.confirm("org-online", [("signing", ["ev-sign"], []), ("health", ["ev-health-online"], [])])

    def test_successors_receive_independent_certificates(self):
        a = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        b = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        self.assertNotEqual(a["cert_id"], b["cert_id"])
        cert_a = self.fx.svc.get_certificate(a["cert_id"])
        cert_b = self.fx.svc.get_certificate(b["cert_id"])
        self.assertEqual(cert_a["parent_cert_id"], "cert-1")
        self.assertEqual(cert_b["parent_cert_id"], "cert-1")
        self.assertEqual(cert_a["holder_org_id"], "org-venue")
        self.assertEqual(cert_b["holder_org_id"], "org-online")
        self.assertEqual(cert_a["versions"][0]["included_components"][0]["responsible_party_org_id"], "org-venue")

    def test_same_plan_replay_does_not_reissue(self):
        first = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        replay = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["cert_id"], first["cert_id"])
        self.assertEqual(replay["version"], 1)
        cert = self.fx.svc.get_certificate(first["cert_id"])
        self.assertEqual(len(cert["versions"]), 1)
        issue_facts = [f for f in self.fx.svc.facts() if f["type"] == "CertificationIssued"]
        self.assertEqual(len(issue_facts), 1)

    def test_idempotency_key_replays_first_result(self):
        first = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"},
                            key="key-issue-1")
        replay = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"},
                             key="key-issue-1")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["cert_id"], first["cert_id"])

    def test_content_change_invalidates_confirmation_and_creates_new_version(self):
        first = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        cert_id = first["cert_id"]
        # 内容变化（新增人脸双录控制）进入复审
        self.fx.cmd(
            "amend_succession_declaration",
            {"split_id": "split-1", "successor_org_id": "org-online",
             "items": [
                 {"component_id": "signing", "controls": ["电子签", "人脸双录"], "gaps": [],
                  "staff_ids": [], "facility_ids": []},
                 {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
                  "controls": ["远程问诊"], "gaps": [], "staff_ids": [], "facility_ids": []},
             ]},
        )
        with self.assertRaises(DomainError) as error:
            self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        self.assertEqual(error.exception.code, "CONFIRMATIONS_STALE")
        self.fx.confirm("org-online", [("signing", ["ev-sign"], []), ("health", ["ev-health-online"], [])])
        second = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["cert_id"], cert_id)
        cert = self.fx.svc.get_certificate(cert_id)
        self.assertEqual([v["status"] for v in cert["versions"]], ["superseded", "active"])
        self.assertEqual(cert["current_version"], 2)
        # 再次重放新方案同样幂等
        replay = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.fx.svc.get_certificate(cert_id)["current_version"], 2)

    def test_suspended_scope_can_join_in_a_later_version(self):
        # 先制造 stay 的严重事件，venue 无法签发；闭环后重审签发 v1
        self.fx.cmd("register_severe_incident",
                    {"split_id": "split-1", "incident_id": "inc-9", "component_ids": ["stay"],
                     "inseparable": True, "description": "x"})
        # online 先拿到独立证书
        online = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        self.assertEqual(online["included_components"], ["signing", "health"])
        # venue 此时无范围；闭环后签发，不影响 online
        with self.assertRaises(DomainError):
            self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        self.fx.cmd("close_severe_incident",
                    {"split_id": "split-1", "incident_id": "inc-9", "resolution": "done"})
        venue = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        self.assertEqual(venue["included_components"], ["stay"])
        self.assertNotEqual(venue["cert_id"], online["cert_id"])


class PromotionTest(unittest.TestCase):
    def setUp(self):
        self.fx = SplitFixture()
        self.fx.open_split()
        self.fx.declare_both()
        self.fx.confirm("org-online", [("signing", ["ev-sign"], []), ("health", ["ev-health-online"], [])])
        self.issued = self.fx.cmd("issue_certification",
                                  {"split_id": "split-1", "successor_org_id": "org-online"})

    def test_new_promotion_must_pin_specific_active_version(self):
        # 冻结原证书不得用于新宣传
        with self.assertRaises(DomainError) as error:
            self.fx.cmd("register_promotion_reference",
                        {"promotion_id": "promo-old", "cert_id": "cert-1", "version": 1})
        self.assertEqual(error.exception.code, "CERT_FROZEN")
        # 必须指向存在的版本
        with self.assertRaises(DomainError) as error:
            self.fx.cmd("register_promotion_reference",
                        {"promotion_id": "promo-x", "cert_id": self.issued["cert_id"], "version": 99})
        self.assertEqual(error.exception.code, "UNKNOWN_VERSION")
        result = self.fx.cmd("register_promotion_reference",
                             {"promotion_id": "promo-1", "cert_id": self.issued["cert_id"], "version": 1})
        self.assertEqual(result["status"], "registered")

    def test_promotion_tracks_superseded_and_partial_revocation(self):
        self.fx.cmd("register_promotion_reference",
                    {"promotion_id": "promo-1", "cert_id": self.issued["cert_id"], "version": 1})
        self.fx.cmd("register_promotion_reference",
                    {"promotion_id": "promo-health", "cert_id": self.issued["cert_id"],
                     "version": 1, "component_ids": ["health"]})
        self.fx.cmd(
            "amend_succession_declaration",
            {"split_id": "split-1", "successor_org_id": "org-online",
             "items": [
                 {"component_id": "signing", "controls": ["电子签", "双录"], "gaps": []},
                 {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
                  "controls": ["远程问诊"], "gaps": []},
             ]},
        )
        self.fx.confirm("org-online", [("signing", ["ev-sign"], []), ("health", ["ev-health-online"], [])])
        self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        self.assertEqual(self.fx.svc.get_promotion("promo-1")["validity"], "superseded")
        # v1 上的部分撤销同样以追加事实保存，并反映到宣传引用状态
        self.fx.cmd("revoke_certification",
                    {"cert_id": self.issued["cert_id"], "component_ids": ["health"], "reason": "投诉激增"})
        self.assertEqual(self.fx.svc.get_promotion("promo-health")["validity"], "scope_partially_revoked")

    def test_revoked_cert_cannot_be_promoted_or_reissued(self):
        self.fx.cmd("revoke_certification", {"cert_id": self.issued["cert_id"], "reason": "严重违规"})
        with self.assertRaises(DomainError) as error:
            self.fx.cmd("register_promotion_reference",
                        {"promotion_id": "promo-r", "cert_id": self.issued["cert_id"], "version": 1})
        self.assertEqual(error.exception.code, "CERT_REVOKED")
        with self.assertRaises(DomainError) as error:
            self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        self.assertEqual(error.exception.code, "CERT_REVOKED")


class ConsumerTest(unittest.TestCase):
    def setUp(self):
        self.fx = SplitFixture()
        self.fx.cmd("record_purchase", {"consumer_id": "u1", "cert_id": "cert-1", "items": [
            {"component_id": "stay", "commitment": "原合同价与房型"},
            {"component_id": "health", "commitment": "原随访频次"},
        ]})
        self.fx.cmd("record_purchase", {"consumer_id": "u2", "cert_id": "cert-1", "items": [
            {"component_id": "signing", "commitment": "原退费规则"},
        ]})
        self.fx.open_split()
        self.fx.declare_both()
        self.fx.confirm("org-venue", [("stay", ["ev-stay"], [])])
        self.fx.confirm("org-online", [("signing", ["ev-sign"], []), ("health", ["ev-health-online"], [])])

    def test_existing_consumers_keep_commitments_and_get_change_records(self):
        venue = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        online = self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        u1 = self.fx.svc.get_consumer("u1")
        # 原购买承诺原样保留
        self.assertEqual(u1["purchases"][0]["items"][0]["commitment"], "原合同价与房型")
        changes = {tuple(n["component_ids"]): n for n in u1["responsibility_changes"]}
        self.assertIn(("stay",), changes)
        self.assertIn(("health",), changes)
        stay_change = changes[("stay",)]
        self.assertEqual(stay_change["old_responsible_party_org_id"], "org-original")
        self.assertEqual(stay_change["new_responsible_party_org_id"], "org-venue")
        self.assertEqual(stay_change["new_cert_id"], venue["cert_id"])
        self.assertTrue(stay_change["commitments_preserved"])
        self.assertFalse(stay_change["delivered"])
        # u2 只涉及 signing，仅收到一条
        u2 = self.fx.svc.get_consumer("u2")
        self.assertEqual([n["new_cert_id"] for n in u2["responsibility_changes"]], [online["cert_id"]])
        # 送达确认追加事实，重放幂等
        notice_id = u2["responsibility_changes"][0]["notice_id"]
        ack = self.fx.cmd("acknowledge_notice", {"consumer_id": "u2", "notice_id": notice_id})
        self.assertTrue(ack["delivered"])
        again = self.fx.cmd("acknowledge_notice", {"consumer_id": "u2", "notice_id": notice_id})
        self.assertTrue(again.get("replayed"))

    def test_new_version_does_not_duplicate_responsibility_change(self):
        self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        self.fx.cmd(
            "amend_succession_declaration",
            {"split_id": "split-1", "successor_org_id": "org-online",
             "items": [
                 {"component_id": "signing", "controls": ["电子签", "双录"], "gaps": []},
                 {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
                  "controls": ["远程问诊"], "gaps": []},
             ]},
        )
        self.fx.confirm("org-online", [("signing", ["ev-sign"], []), ("health", ["ev-health-online"], [])])
        self.fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
        u1 = self.fx.svc.get_consumer("u1")
        health_notices = [n for n in u1["responsibility_changes"] if n["component_ids"] == ["health"]]
        self.assertEqual(len(health_notices), 1)


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.fx = SplitFixture()
        self.fx.cmd("record_purchase", {"consumer_id": "u1", "cert_id": "cert-1", "items": [
            {"component_id": "health", "commitment": "原随访频次"}]})
        self.fx.open_split()
        self.fx.declare_both()
        self.fx.confirm("org-venue", [("stay", ["ev-stay"], [])])
        self.fx.confirm("org-online", [("signing", ["ev-sign"], []), ("health", ["ev-health-online"], [])])
        self.venue = self.fx.cmd("issue_certification",
                                 {"split_id": "split-1", "successor_org_id": "org-venue"})
        self.online = self.fx.cmd("issue_certification",
                                  {"split_id": "split-1", "successor_org_id": "org-online"})

    def test_new_cert_traces_to_source_evidence_and_exclusion_reasons(self):
        lineage = self.fx.svc.lineage(self.online["cert_id"])
        self.assertEqual(lineage["source_cert_id"], "cert-1")
        inherited = {(i["component_id"], tuple(sorted(e["evidence_id"] for e in i["inherited_evidence"])))
                     for i in lineage["inheritance"]}
        self.assertIn(("signing", ("ev-sign",)), inherited)
        self.assertIn(("health", ("ev-health-online",)), inherited)
        reasons = {(n["component_id"], n["evidence_id"]): n["reason_code"]
                   for n in lineage["not_inherited_evidence"]}
        self.assertEqual(reasons[("health", "ev-health-beijing")], "outside_applicability")
        # 冻结快照明细可回溯
        self.assertEqual([c["id"] for c in lineage["source_snapshot"]["components"]],
                         ["stay", "signing", "health"])
        # 历史消费者处置可从新证书追到
        consumers = {c["consumer_id"]: c for c in lineage["consumers"]}
        self.assertIn("u1", consumers)
        self.assertTrue(consumers["u1"]["commitments_preserved"])
        self.assertEqual(consumers["u1"]["responsibility_changes"][0]["new_cert_id"],
                         self.online["cert_id"])

    def test_source_cert_lists_independent_successors(self):
        lineage = self.fx.svc.lineage("cert-1")
        self.assertEqual({s["cert_id"] for s in lineage["successors"]},
                         {self.venue["cert_id"], self.online["cert_id"]})
        self.assertEqual({s["holder_org_id"] for s in lineage["successors"]},
                         {"org-venue", "org-online"})

    def test_revocations_are_appended_facts(self):
        cert_id = self.online["cert_id"]
        self.fx.cmd("revoke_certification",
                    {"cert_id": cert_id, "component_ids": ["health"], "reason": "r"})
        self.fx.cmd("revoke_certification", {"cert_id": cert_id, "reason": "撤销全部"})
        types = [f["type"] for f in self.fx.svc.facts()]
        self.assertIn("CertificationScopePartiallyRevoked", types)
        self.assertIn("CertificationRevoked", types)
        self.assertEqual(self.fx.svc.get_certificate(cert_id)["status"], "revoked")
        # 撤销不抹除历史版本与溯源
        lineage = self.fx.svc.lineage(cert_id)
        self.assertTrue(any(i["component_id"] == "health" for i in lineage["inheritance"]))


class AppendOnlyTest(unittest.TestCase):
    def test_all_state_changes_are_facts_with_monotonic_seq(self):
        fx = SplitFixture()
        fx.open_split()
        fx.declare_both()
        fx.confirm("org-venue", [("stay", ["ev-stay"], [])])
        fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        facts = fx.svc.facts()
        self.assertEqual([f["seq"] for f in facts], list(range(1, len(facts) + 1)))
        # 事实只增不改：重放同一方案后事实数不变
        before = len(facts)
        fx.cmd("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
        self.assertEqual(len(fx.svc.facts()), before)


if __name__ == "__main__":
    unittest.main()
