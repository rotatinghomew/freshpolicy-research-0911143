import json
import tempfile
import threading
from urllib.request import Request, urlopen

from freshpolicy.service import Application, make_server


def main():
    with tempfile.TemporaryDirectory() as state:
        server = make_server(Application("data", state), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            for path in ("/health", "/tables", "/view"):
                with urlopen(base + path, timeout=5) as response:
                    print(path, json.dumps(json.load(response), ensure_ascii=False))
            request = Request(base + "/cases", data=json.dumps({"label": "月度经营讨论", "options": {"period": "2026-09"}}, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                created = json.load(response)
            with urlopen(base + "/cases/" + created["id"], timeout=5) as response:
                restored = json.load(response)
            assert created == restored
            print("研究记录已回读", restored["id"], restored["source_fingerprint"])
            trial_body = json.dumps({"label": "补货规则试算", "scenarios": 4}, ensure_ascii=False).encode()
            request = Request(base + "/trials", data=trial_body, headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=30) as response:
                trial = json.load(response)
            headline = {name: metrics["contribution"]["mean"] for name, metrics in trial["report"]["bootstrap"]["rules"].items()}
            print("试算经营贡献均值", json.dumps(headline, ensure_ascii=False))
            with urlopen(base + f"/trials/{trial['id']}/trace?rule=intent_average&scenario=1", timeout=30) as response:
                trace = json.load(response)
            print("追溯天数", len(trace["days"]), "首日", trace["days"][0]["date"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
