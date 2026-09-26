# 生物样本库知情同意与撤回

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8302`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8302
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `participant`：参与者；`consent`：同意版本；`sample`：样本；`withdrawal`：撤回申请。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 撤回执行（`withdrawal` 的 `execute` 动作）

执行撤回是一次完整处置，不再需要工作人员逐条维护样本：

- 执行前按撤回申请的`participant_id`重新核对批准时记录的每份`sample_ids`样本；混入他人样本、样本已处置（如`destroyed`、`anonymized`）或状态不明（非`stored`/`on_loan`，包括样本已消失）时整批停止，申请和样本均保持原状。
- 核对通过后在单个数据库事务内原子完成：在库（`stored`）样本销毁（`destroyed`），借出（`on_loan`）样本转入待召回（`pending_recall`）。任何一份样本处置失败，全部回滚，不会只成功一半。
- 每份样本的数据中都写入`withdrawal_id`、`withdrawal_disposition`（`destroyed`/`pending_recall`）和`withdrawn_at`（即申请的`executed_at`），并分别产生`destroy`/`recall`审计记录；撤回申请的`sample_results`保存逐样本结果。
- 同一申请重复执行时直接返回已有的执行结果：不重新记`executed_at`、不重复销毁或召回样本、不新增审计记录。


## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
