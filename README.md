# ICBC 路考抢位（Burnaby Claims Centre, 4399 Wayburne Dr）

`icbc_roadtest.py` 直接调用 ICBC 路考预约网站背后的接口（`onlinebusiness.icbc.com/deas-api/v1`，接口格式是从 webdeas-ui 前端代码里扒出来的），完整流程是：登录 → 查空位 → 锁位 → 从 Gmail 自动读取验证码 → 预约。时段比现有预约更好、并且改期还免费的时候，会自动改期。

## 一次性设置

1. 安装依赖：`pip3 install -r requirements.txt`
2. `.env` 里已经填好 ICBC 登录信息，文件权限是 600，并且已加入 `.gitignore`
3. **Gmail 应用专用密码**：先开启两步验证，然后到 <https://myaccount.google.com/apppasswords> 生成一个，填进 `GMAIL_APP_PASSWORD`
4. （可选）`NTFY_TOPIC`：在手机 ntfy app 里订阅同名 topic，抢到号时会推送到手机

## 在 GitHub Actions 上跑（不用开着电脑）

`.github/workflows/grab.yml` 沿用 icbc-wed-grab 的接力方案：每次运行约 5.75 小时，结束时自己触发下一次，cron 只做兜底。抢到最高档或者进入收费期后（退出码 10），工作流会自动停用自己。

1. 在 `.env` 里填好 `GMAIL_APP_PASSWORD`（建议同时填上 `NTFY_TOPIC`，因为 GitHub 上收不到 Mac 通知）
2. 用**公开仓库**：私有仓库每月只有 2,000 免费分钟，跑不完 8 天。日志里不会打印姓名和 ID，Secrets 会被自动打码
3. 把 `.env` 一次性导入为仓库 Secrets（由你自己执行）：`gh secret set -f .env --repo <owner>/<repo>`
4. Actions → grab-roadtest → Run workflow，在 args 里填 `--list`，看 ICBC 会不会屏蔽 GitHub 的 IP、考点和考试类别对不对
5. 再跑一次，args 填 `--trial --yes`，验证完整的订号、验证码、取消流程
6. 最后 args 留空跑一次，正式开始接力抢号

Secrets 没设置好时，工作流不会抢号，也不会接力；运行失败时会等 5 分钟再触发下一次，避免出错后无限空转。

## 本地用法

```bash
python3 icbc_roadtest.py --list            # 登录 + 列出所有空位和档位，不做任何预约
python3 icbc_roadtest.py --trial           # 实验：订一个窗口外且 72 小时后的号，确认后立刻取消
caffeinate -i python3 icbc_roadtest.py     # 正式抢号（caffeinate 防止 Mac 睡眠）
python3 icbc_roadtest.py --dry-run --once  # 只看会选哪个，不锁位
python3 icbc_roadtest.py --cancel          # 取消当前路考预约
```

## 策略

- 时间窗口：10/24（周六）12:00 到 10/30（周五）0:00，不含周五。可以用 `--start` / `--end` 修改
- 时段档位（中午优先）：工作日 10:00–13:30 > 9:30–10:00、13:30–14:15 > 9:30 之前 > 周末；工作日 14:15 以后**不订**（放学时段接晚高峰）
- 已经订到号之后，只有遇到**更高一档**的时段才改期，而且原预约必须还在 48 小时 + 1 小时以外（ICBC 在 48 小时内改期或取消收 $25）。拿到最高档，或者进入收费期，脚本就自动结束
- 刷新频率：23:58–00:06 每 8 秒（0 点集中放号，临近 0 点会先刷新登录）；22:00–23:58 和 05:55–07:10 每 20 秒；其他时间每 60 秒。可以用 `--busy` / `--interval` 修改

## 未经实网验证的地方（`--list` 和 `--trial` 跑一次就能确认）

- 验证码邮件的格式（发件人里应该含 icbc、正文里有 code 字样）；如果自动识别不到，终端也可以手动输入
- `/web/book`、`/web/rebook` 的返回内容；脚本会重新登录，核对账户里的预约来确认是否订上
- 每次请求的原始返回都保存在 `icbc_debug/`（含个人信息，已 gitignore）
