"""拆分承接能力的 HTTP 契约：POST /rpc 命令入口与只读查询路由。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from certification import CertificationService
from service import build_handler


def post_json(base_url, payload):
    request = Request(
        f"{base_url}/rpc",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    return json.load(urlopen(request, timeout=3))


class HttpSplitContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = CertificationService()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(cls.service))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _get(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=3) as response:
            return response.status, json.load(response)

    def _post(self, command, params=None, key=None):
        payload = {"command": command, "params": params or {}}
        if key is not None:
            payload["idempotency_key"] = key
        return post_json(self.base_url, payload)["result"]

    def _expect_error(self, command, params=None, status=400):
        try:
            post_json(self.base_url, {"command": command, "params": params or {}})
        except HTTPError as error:
            self.assertEqual(error.code, status)
            body = json.load(error)
            error.close()
            return body["error"]
        self.fail("应当返回领域错误")

    def test_full_split_flow_over_http(self):
        # 登记
        for oid, name in [("org-original", "原机构"), ("org-online", "线上公司")]:
            self._post("register_organization", {"org_id": oid, "name": name})
        self._post("register_certificate", {
            "cert_id": "cert-h", "holder_org_id": "org-original",
            "valid_from": "2025-01-01", "valid_to": "2027-01-01",
            "components": [{
                "id": "signing", "name": "线上签约",
                "populations": ["self-care"], "locations": ["online"],
                "responsible_party_org_id": "org-original",
            }],
        })
        self._post("record_evidence", {
            "evidence_id": "ev-h", "cert_id": "cert-h", "component_id": "signing",
            "populations": ["self-care"], "locations": ["online"], "summary": "测评",
        })

        # 冻结与承接
        self._post("open_split", {"split_id": "split-h", "cert_id": "cert-h"})
        status, cert = self._get("/certificates/cert-h")
        self.assertEqual(status, 200)
        self.assertEqual(cert["status"], "frozen")

        declared = self._post("declare_succession", {
            "split_id": "split-h", "successor_org_id": "org-online",
            "items": [{"component_id": "signing", "controls": ["电子签"], "gaps": []}],
        })
        self.assertEqual(declared["evidence_candidates"]["signing"]["applicable"], ["ev-h"])

        status, split = self._get("/splits/split-h")
        self.assertEqual(status, 200)
        self.assertEqual(split["frozen"]["holder_org_id"], "org-original")

        self._post("confirm_experience_coverage", {
            "split_id": "split-h", "successor_org_id": "org-online", "reviewer": "r1",
            "decisions": [{"component_id": "signing", "accepted_evidence_ids": ["ev-h"]}],
        })
        self._post("confirm_responsibility_handover", {
            "split_id": "split-h", "successor_org_id": "org-online", "compliance_officer": "c1",
            "decisions": [{"component_id": "signing", "complaint_channel": "app",
                           "liability_acknowledged": True}],
        })
        issued = self._post("issue_certification",
                            {"split_id": "split-h", "successor_org_id": "org-online"})
        new_cert_id = issued["cert_id"]

        # 幂等键重放
        replay = post_json(self.base_url, {
            "command": "issue_certification",
            "params": {"split_id": "split-h", "successor_org_id": "org-online"},
            "idempotency_key": "issue-once",
        })["result"]
        replay_again = post_json(self.base_url, {
            "command": "issue_certification",
            "params": {"split_id": "split-h", "successor_org_id": "org-online"},
            "idempotency_key": "issue-once",
        })["result"]
        self.assertEqual(replay["cert_id"], new_cert_id)
        self.assertTrue(replay_again["idempotent_replay"])

        # 溯源与版本查询
        status, lineage = self._get(f"/certificates/{new_cert_id}/lineage")
        self.assertEqual(lineage["source_cert_id"], "cert-h")
        self.assertEqual(lineage["inheritance"][0]["inherited_evidence"][0]["evidence_id"], "ev-h")
        status, versions = self._get(f"/certificates/{new_cert_id}/versions")
        self.assertEqual(versions["versions"][0]["version"], 1)

        # 宣传必须钉住明确版本
        self._post("register_promotion_reference",
                   {"promotion_id": "promo-h", "cert_id": new_cert_id, "version": 1})
        status, promotion = self._get("/promotions/promo-h")
        self.assertEqual(promotion["validity"], "valid")

        # 审计事实流可读
        status, facts = self._get("/facts")
        self.assertIn("SplitOpened", [f["type"] for f in facts["facts"]])

    def test_domain_and_protocol_errors_are_structured(self):
        error = self._expect_error("not_a_command", {}, status=404)
        self.assertEqual(error["code"], "UNKNOWN_COMMAND")
        # 错误的 JSON
        request = Request(
            f"{self.base_url}/rpc",
            data=b"{not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request, timeout=3)
        except HTTPError as e:
            self.assertEqual(e.code, 400)
            self.assertEqual(json.load(e)["error"]["code"], "INVALID_JSON")
        else:
            self.fail("非法 JSON 应被拒绝")

        # 未知 GET 路由仍是 404
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/nope", timeout=3)
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
