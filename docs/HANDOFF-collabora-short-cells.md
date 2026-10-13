# 修复记录 — Collabora 表格翻译短单元格不翻译

> 症状：Collabora Online 表格框选翻译时，短中文单元格（1–5 字）大面积"原样不翻"，
> 只有长文本格被翻译。根因定位为服务端两个问题，**与 Collabora 引擎无关，与镜像无关**。
> 修复已合入本仓库（见下方"修复方案"各节的实现状态）。

---

## 一、现象

用户在 Collabora Online 中框选中文表格区域 → 工具 → 翻译 → 德语/英语，结果：

- 11 字以上的长单元格正常翻译；
- 1–5 字的短单元格（如「类别和属性」「文件编号」「保密密级」「发放范围」「秘密」「质量部」）**全部保持中文原文**；
- 引擎无报错，服务返回 HTTP 200，日志显示 `DeepL served 1/1 items`（被判定为成功）。

## 二、根因（两条，相互独立但共同导致症状）

### 根因 1（主因）：服务端没读 POST body 里的 `tag_handling`

Collabora 引擎把 `tag_handling=html` 放在 **POST body**（`application/x-www-form-urlencoded`），
**不是** URL query string。已用日志 mock 抓包实证：

```
REQUEST LINE: POST /translate
QUERY STRING: ''                                              ← 空
BODY: target_lang=DE&tag_handling=html&text=%3Cspan...        ← 在 body
```

而 `main.py` 的 `/translate` 端点原先只读 query string：

```python
tag_handling = request.query_params.get("tag_handling", "plain")   # body 里的完全没读
```

→ 引擎的 `tag_handling=html` 被忽略 → 落回默认 `plain` → 走 `bridge.translate_plain`
（**不剥离 HTML 标签**，把整串嵌套标记当纯文本送 DeepL）。

### 根因 2（放大器）：DeepL 上游对短中文 + 一堆标签原样返回，且 identity 不触发 LLM 兜底

plain 路径把这样的整串直接送 DeepL：

```html
<span align="center" style="..."><font face="宋体"><span lang="zh-CN"><font face="宋体"><span lang="zh-CN"><b>类别和属性</b></span></font></span></font></span>
```

DeepL 对 1–5 字短中文片段高比例**原样返回（identity）**。72 小时 1127 条真实请求统计：

| 模式 | 1-2字 | 3-5字 | 6-10字 | 11+字 |
|---|---|---|---|---|
| html | 98% 没翻 | 0% | 1% | 0% |
| plain | 83% 没翻 | 27% | 52% | 4% |

关键缺陷：identity（返回==原文）被当作**成功**（`DeepL served 1/1 items`），
**不会**触发 `translate_items` 里的 `except → LLM` 兜底——LLM 一次都没机会接手短格。

## 三、修复方案（已实现）

### 修复 A：解析 body 里的 `tag_handling` —— ✅ 已实现

`main.py` `/translate` 端点改为（query > body > 默认）：

```python
tag_handling = (request.query_params.get("tag_handling")
                or data.get("tag_handling")        # 引擎放在 body(form/json)
                or "plain")
```

效果：引擎请求被识别为 html → 走 `deepl_translate_html_raw` → DeepL 保留标签结构正常翻译。

**验证命令**（应返回保留标签的译文，且日志出现 `[REQ] tag_handling=html` + `[DEEPL-RAW-REQ]`）：

```bash
curl -X POST "https://<your-bridge-host>/translate" \
  -d "auth_key=$BRIDGE_TOKEN" \
  --data-urlencode "text=<span><b>文件编号</b></span>" \
  --data-urlencode "target_lang=DE" \
  --data-urlencode "tag_handling=html"
# 期望: {"translations":[{"text":"<span><b>Aktenzeichen</b></span>"}]}
```

### 修复 B：identity 时 fallback LLM —— ✅ 已实现

`translate_items` 在 DeepL 成功返回后做 identity 检测：某条译文剥离 HTML 标签后
与原文完全相同且含 CJK 字符（`[一-鿿぀-ヿ가-힯]`，覆盖中日韩），则整批改走
LLM 兜底。`deepl_translate_html_raw` 对单条 HTML 原文做同样检测。

非 CJK 的 identity（数字、专有名词、"COVID-19" 这类本来就不用翻的）**不**触发兜底，
避免浪费 LLM 往返。

示意（实际实现见 `main.py`）：

```python
def _strip_html(t): return re.sub(r"<[^>]+>", "", t).strip()
def _has_cjk(t): return bool(re.search(r"[一-鿿぀-ヿ가-힯]", t))

out = deepl_translate_items(texts, target_lang, tag_handling)
if any(_strip_html(o) == _strip_html(t) and _has_cjk(_strip_html(t))
       for t, o in zip(texts, out)):
    raise RuntimeError("identity")   # 复用现有 except → LLM 分支
return out
```

效果：即使 html 模式下 DeepL 对个别短格仍返回原文，LLM 也能兜底，彻底消除"短格不翻"。

### 修复 C（可选，未做）：plain 路径剥标签

`bridge.translate_plain` 可在送 DeepL 前先 `extract_text_nodes` 提取纯文字、翻译后
`refill`，避免格式标签干扰 DeepL。修复 A 之后引擎已走 html 路径，C 仅对其它 plain
调用方有意义，优先级低。

## 四、验证清单

- [x] 修复 A：body `tag_handling=html` 被识别（`test_tag_handling_html_in_form_body` 等单测）
- [x] 修复 B：DeepL identity → LLM 兜底（`test_deepl_identity_cjk_falls_back_to_llm` 等单测）
- [x] 不误伤：真实翻译 / 非 CJK identity 不触发兜底（对应两条单测）
- [ ] 生产回归：Collabora 表格框选翻译，短格翻成目标语言；日志出现
      `[REQ] tag_handling=html` + `[DEEPL-RAW-REQ]`，个别短格出现
      `identity; falling back to LLM`

## 五、已排除的非根因（有据）

- **Collabora 引擎 bug**：引擎逐格导出 HTML、粘贴译文逻辑经像素级 + 落盘双重验证正确
  （6 格、20 格、52×4=212 格、封面 66 格全部正确替换）。
- **镜像不一致**：全部生产 pod 的 imageID 与测试容器逐字节一致。
- **反向代理丢 query string**：外网带 `?tag_handling=html` 实测，服务日志正确收到该参数。
- **服务不稳定**：72h 1123 条 POST 全部 HTTP 200，仅 1 次 SSL 握手超时（已正确 fallback LLM）。
- **浏览器 console 报错**：均为 Nextcloud viewer/tiptap/扩展 404 噪音，与翻译链路无关。

## 六、引擎请求格式参考

- `POST /translate`，`Content-Type: application/x-www-form-urlencoded`
- body 含 `target_lang` / `tag_handling=html` / `text`（URL-encoded HTML）
- Collabora 通过 `coolwsd.xml` 的 `deepl.api_url` 指向本服务的 `/translate`
