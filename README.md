# freshpolicy 海鲜经营资料服务

这个 Python 项目为经营研究提供本地 CSV 资料、领域视图、数值剖面、不可变研究备注和补货策略试算。现有经营资料的字段与计量语义见 `docs/domain.md`，试算口径见 `docs/simulation.md`，HTTP 行为见 `docs/api.md`。服务不连接真实经营系统，也不自动执行交易或订货。

## 目录

`freshpolicy/catalog.py` 解析并校验资料，`domain.py` 提供当前经营视图，`profile.py` 提供探索性数值摘要，`cases.py` 保存含资料副本的研究备注，`simulation.py` 是逐日逐批的补货模拟引擎与候选规则，`evaluation.py` 负责拟合、情景生成、规则比较与追溯，`trials.py` 保存不可变试算记录，`service.py` 处理 HTTP。`data/` 是可重复使用的虚构经营资料，`tests/` 检查当前接口和领域口径，`smoke.py` 发起真实本地 HTTP 请求后退出。

## 本机运行

使用 Python 3.11，依赖版本固定在 `requirements.txt`。首次准备依赖后，运行和验证不访问公网。

```bash
python3 -m pip install -r requirements.txt
python3 -m freshpolicy
```

默认资料目录为 `data`，研究备注与试算记录写入 `state`；入口支持 `--data`、`--state`、`--host` 和 `--port`。研究记录含完整源表副本和内容校验，不会因源文件后续更改自动变成新的研究记录。

## 策略试算

`POST /trials` 在历史留时段上比较补货规则：拟合只用留时段之前的资料，同一组种子情景（需求意向、供应商日共同延误与容量、到货剩余保质期）供全部规则复用，模拟逐批先到期先出、退货报废与跨日结转，输出缺货、满足率、损耗与经营贡献的联合结果和配对差异区间，并附历史回放、极端供货情景与需求假设区间下的敏感性。`GET /trials/{id}/trace` 追到具体日期和批次的库存、资金与损耗变化。试算只保存规格与报告，不替业务下单。

```bash
curl -s -X POST http://127.0.0.1:8080/trials -d '{"label": "九月留时段试算", "scenarios": 32}'
curl -s "http://127.0.0.1:8080/trials/<id>/trace?rule=intent_average&scenario=1"
```

## 测试和编译

在项目根目录执行，测试会自行创建临时状态目录并清理，HTTP 测试由系统分配空闲端口。

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q freshpolicy tests smoke.py
python3 -m build --wheel --no-isolation
python3 smoke.py
```

## 容器运行

镜像固定 Python 基础镜像内容摘要及数值依赖版本，环境准备阶段需要包镜像。验证阶段可完全关闭网络，仅在容器内访问环回 HTTP。

```bash
docker build -t freshpolicy:local .
docker run --rm --network none freshpolicy:local python -m unittest discover -s tests -v
docker run --rm --network none freshpolicy:local python smoke.py
docker run --rm -p 127.0.0.1:8080:8080 freshpolicy:local
```

完整接口检查通常只需数秒。数值剖面的金额转为浮点仅为探索用途，成本金额对账仍以原始十进制字段为准。
