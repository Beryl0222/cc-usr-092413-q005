"""基础服务与拆分承接 HTTP 接口的端到端合同测试。"""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import (
    SERVICE_ID,
    SERVICE_NAME,
    create_handler_class,
    health_payload,
)


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler_class())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, payload=None, expect_error=False):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{path}", data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            body = json.load(error)
            error.close()
            if expect_error:
                return error.code, body
            raise AssertionError(f"{method} {path} 失败: {error.code} {body}") from error


class HealthContractTest(HttpTest):
    def test_health_payload_has_stable_identity(self):
        self.assertEqual(
            health_payload(),
            {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME},
        )

    def test_health_endpoint_returns_json(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())

    def test_unknown_route_is_not_exposed(self):
        status, _ = self.request("GET", "/unknown", expect_error=True)
        self.assertEqual(status, 404)


class SplitFlowContractTest(HttpTest):
    """走通冻结→声明→候选→双轨确认→签发→引用→追溯的完整 HTTP 流程。"""

    def test_full_split_flow_over_http(self):
        # 1. 登记原证书、证据、无法分割的严重事件、已购用户
        _, cert = self.request("POST", "/certificates", {
            "certificate_id": "cert-http",
            "holder": "连锁康养机构",
            "responsible_party": "原连锁康养机构",
            "valid_until": "2027-09-09",
            "components": [
                {"id": "stay", "name": "旅居住宿"},
                {"id": "health", "name": "健康支持"},
            ],
            "population_tiers": [{"id": "self_care", "name": "自理"}],
            "locations": ["三亚基地"],
        })
        self.assertEqual(cert["status"], "active")

        for component in ("stay", "health"):
            self.request("POST", "/evidence", {
                "evidence_id": f"ev-{component}", "certificate_id": "cert-http",
                "component_id": component, "summary": f"{component} 体验回访",
                "population_tiers": ["self_care"], "locations": ["三亚基地"],
            })
        self.request("POST", "/events", {
            "event_id": "evt-http", "certificate_id": "cert-http",
            "kind": "shared_staff", "summary": "护理主管共用且排班未切割",
            "component_ids": ["health"], "severable": False,
        })
        self.request("POST", "/consumers", {
            "consumer_id": "csm-http", "certificate_id": "cert-http",
            "component_id": "stay", "commitment": "原旅居权益至2027年",
        })

        # 2. 冻结
        _, frozen = self.request("POST", "/splits/freeze", {"certificate_id": "cert-http"})
        self.assertTrue(frozen["frozen"])
        self.assertEqual(frozen["facts"][0]["type"], "freeze")

        # 3. 发起拆分
        _, split = self.request("POST", "/splits", {
            "certificate_id": "cert-http",
            "venue_subject_id": "venue-co", "online_subject_id": "online-co",
        })
        split_id = split["split_id"]

        # 4. 两主体分别声明承接项、控制措施与缺口
        self.request("POST", "/splits/declarations", {
            "split_id": split_id, "subject_id": "venue-co",
            "component_ids": ["stay"],
            "controls": ["场地消防巡检", "持证护理员在岗"],
            "liability_accepted": True, "complaint_handling_accepted": True,
        })
        self.request("POST", "/splits/declarations", {
            "split_id": split_id, "subject_id": "online-co",
            "component_ids": ["health"],
            "controls": ["健康档案加密"],
            "gaps": [{"component_id": "health", "detail": "线下应急联动待建"}],
            "liability_accepted": True, "complaint_handling_accepted": True,
        })

        # 5. 系统提出可继承候选
        _, candidates = self.request("POST", "/splits/candidates", {"split_id": split_id})
        self.assertEqual(candidates["venue-co"]["stay"]["inheritable_evidence"], ["ev-stay"])

        # 6. 评审员确认体验覆盖、合规确认责任与投诉承接（4 次确认）
        for subject in ("venue-co", "online-co"):
            for track, reviewer in (("experience", "reviewer-a"), ("compliance", "legal-b")):
                _, review = self.request("POST", "/reviews/confirm", {
                    "split_id": split_id, "subject_id": subject,
                    "track": track, "decision": "pass", "reviewer": reviewer,
                })
                self.assertEqual(review["review"][track][subject]["decision"], "pass")

        # 7. 签发相互独立的新版本；health 因共用人员暂停，不拖累 stay
        _, result = self.request("POST", "/splits/issue", {"split_id": split_id})
        self.assertFalse(result["replayed"])
        venue_cert = next(c for c in result["issued"] if c["responsible_party"] == "venue-co")
        online_cert = next(c for c in result["issued"] if c["responsible_party"] == "online-co")
        self.assertEqual(venue_cert["status"], "active")
        self.assertEqual(online_cert["status"], "suspended")
        self.assertEqual(online_cert["scope"]["components"], [])
        self.assertEqual(online_cert["suspended_components"], ["health"])

        # 8. 相同方案重放不重复签发
        _, replay = self.request("POST", "/splits/issue", {"split_id": split_id})
        self.assertTrue(replay["replayed"])

        # 9. 宣传引用必须指向明确版本
        _, citation = self.request("POST", "/citations", {
            "certificate_id": venue_cert["certificate_id"],
            "citation": "门店海报认证标识", "registered_by": "market",
        })
        self.assertEqual(citation["version"], venue_cert["version"])
        status, _ = self.request("POST", "/citations", {
            "certificate_id": online_cert["certificate_id"],
            "citation": "暂停范围的宣传", "registered_by": "market",
        }, expect_error=True)
        self.assertEqual(status, 422)

        # 10. 整改解决共用人员后重签，health 恢复
        self.request("POST", "/certificates/remediation", {
            "certificate_id": online_cert["certificate_id"],
            "component_id": "health",
            "summary": "护理主管排班切割完成，新增独立值班长",
            "resolves_events": ["evt-http"],
            "submitted_by": "online-co",
        })
        _, restored = self.request("POST", "/splits/issue", {"split_id": split_id})
        # venue 方案未变，证书复用；online 重签
        online_v2 = next(c for c in restored["issued"] if c["responsible_party"] == "online-co")
        self.assertIn("health", online_v2["scope"]["components"])
        self.assertEqual(online_v2["version"], 2)
        venue_same = next(c for c in restored["issued"] if c["responsible_party"] == "venue-co")
        self.assertEqual(venue_same["certificate_id"], venue_cert["certificate_id"])

        # 11. 从新证书追溯原证据、暂停原因与消费者处置
        _, trace = self.request("GET", f"/trace/{venue_cert['certificate_id']}")
        self.assertEqual(trace["original_snapshot"]["responsible_party"], "原连锁康养机构")
        stay_trace = next(c for c in trace["components"] if c["component_id"] == "stay")
        self.assertEqual(stay_trace["inherited_evidence"], ["ev-stay"])
        self.assertEqual(trace["consumer_dispositions"][0]["consumer_id"], "csm-http")
        self.assertEqual(
            trace["consumer_dispositions"][0]["responsibility_history"][0]["to_subject"],
            "venue-co",
        )

        # 12. 撤销以追加事实保存
        _, revocation = self.request("POST", "/certificates/revoke", {
            "certificate_id": online_v2["certificate_id"],
            "component_id": "health", "reason": "整改后复查再发现问题",
            "revoked_by": "board",
        })
        self.assertEqual(revocation["component_id"], "health")
        _, online_view = self.request("GET", f"/certificates/{online_v2['certificate_id']}")
        self.assertNotIn("health", online_view["scope"]["components"])
        self.assertTrue(any(f["type"] == "revocation" for f in online_view["facts"]))


class ValidationContractTest(HttpTest):
    def test_freeze_before_split_is_rejected(self):
        _, cert = self.request("POST", "/certificates", {
            "certificate_id": "cert-early",
            "holder": "机构", "responsible_party": "机构",
            "valid_until": "2027-01-01",
            "components": [{"id": "stay", "name": "住宿"}],
        })
        status, body = self.request("POST", "/splits", {
            "certificate_id": "cert-early",
            "venue_subject_id": "v", "online_subject_id": "o",
        }, expect_error=True)
        self.assertEqual(status, 422)
        self.assertIn("冻结", body["error"])

    def test_bad_json_returns_400(self):
        request = Request(
            f"{self.base_url}/certificates", data=b"{not-json", method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urlopen(request, timeout=2)
        except HTTPError as error:
            self.assertEqual(error.code, 400)
            error.close()
        else:
            self.fail("应当返回 400")


if __name__ == "__main__":
    unittest.main()
