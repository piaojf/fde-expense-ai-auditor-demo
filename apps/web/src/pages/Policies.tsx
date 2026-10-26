import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Layout from "../components/Layout";
import UploadedPolicyFiles from "../components/UploadedPolicyFiles";
import { ENTERPRISE_MODE } from '../lib/api/enterprise';
import { useAuth } from '../lib/session/AuthContext';
import PolicyPublicationPanel from "../components/PolicyPublicationPanel";
import TravelPolicyPanel from "../components/TravelPolicyPanel";
import { CATEGORY_LABEL } from "../utils/format";
import {
  downloadPolicy,
  policyHttp,
  uploadPolicy,
  type PolicyList,
  type PolicyMetadata,
  type PolicyPreview,
  type PolicyUpload,
  type Clause,
} from "../lib/api/policies";

const LABELS: [keyof PolicyMetadata, string][] = [
  ["title", "制度名称"],
  ["legal_entity", "公司"],
  ["region", "地区"],
  ["expense_type", "费用类型"],
  ["version", "制度版本"],
  ["effective_from", "生效日期"],
  ["effective_to", "失效日期（选填）"],
];
const STATES = {
  PARSED: "已解析 · 待确认",
  CONFIRMED: "制度信息已确认",
  INDEXED: "条款已就绪 · 仍为草稿",
  FAILED: "导入失败 · 可重试",
  DELETED: "已删除",
};
/** 草稿状态 → 视觉标签（仅表示流程进度，不代表已生效） */
const STATE_BADGE: Record<keyof typeof STATES, string> = {
  PARSED: "badge-tag-orange",
  CONFIRMED: "badge-tag-blue",
  INDEXED: "badge-tag-green",
  FAILED: "badge-tag-red",
  DELETED: "badge-tag-gray",
};
const EMPTY: PolicyMetadata = {
  title: "",
  legal_entity: "",
  region: "",
  expense_type: "HOTEL",
  version: "",
  effective_from: "",
  effective_to: null,
};
function initialMetadata(row: PolicyUpload): PolicyMetadata {
  if (row.metadata) return row.metadata;
  const result = { ...EMPTY };
  LABELS.forEach(([key]) => {
    const value = row.parsed.extracted_metadata?.[key]?.value;
    if (value && value !== "UNKNOWN") Object.assign(result, { [key]: value });
  });
  // UNKNOWN category must be explicitly chosen by the administrator.
  if (row.parsed.extracted_metadata?.expense_type?.value === "UNKNOWN")
    result.expense_type = "" as PolicyMetadata["expense_type"];
  if (ENTERPRISE_MODE) {
    result.region = "*";
    result.expense_type = "ALL";
  }
  return result;
}
function ClauseCard({ clause }: { clause: Clause }) {
  return (
    <article className="policy-clause">
      <div className="policy-clause-head">
        <strong>{clause.chunk_id.split(":").pop()}</strong>
        {clause.relevance_score !== undefined && (
          <span>向量相关度 {(clause.relevance_score * 100).toFixed(1)}%</span>
        )}
      </div>
      <pre>{clause.quote}</pre>
      <div className="policy-source">
        来源 {clause.source.source_uri} · 字符 {clause.source.start}–
        {clause.source.end} · {clause.source.embedding_model} /{" "}
        {clause.source.embedding_dimension} 维
      </div>
      <details>
        <summary>对应原始条款与位置</summary>
        {clause.source.lines.map((line, i) => (
          <p key={i}>
            [{line.page ? `第 ${line.page} 页，` : "段落 "}
            {line.line} · {line.method}] {line.text}
          </p>
        ))}
      </details>
    </article>
  );
}
/**
 * 记住凭证：本机演示用的制度管理密钥。
 * 放在 localStorage 以便跨标签页、跨会话复用，不必每次进入制度页重新手输；
 * 「断开访问」或验证失效时立即清除。仍保留原有 sessionStorage 键的读取，
 * 兼容此前已经验证过的会话。
 */
const TOKEN_KEY = "ui-p3a-token";
function readRememberedToken() {
  if (ENTERPRISE_MODE) {
    forgetToken();
    return "";
  }
  try {
    return localStorage.getItem(TOKEN_KEY) ?? sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return ""; // 隐私模式下不可读，按未记住处理
  }
}
function rememberToken(value: string) {
  if (ENTERPRISE_MODE) return;
  try {
    localStorage.setItem(TOKEN_KEY, value);
  } catch {
    /* 不可写时退化为仅本次会话有效 */
  }
}
function forgetToken() {
  try {
    localStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 不可写时忽略 */
  }
}
export default function Policies() {
  const { user } = useAuth();
  if (ENTERPRISE_MODE && user?.role === 'FINANCE') return <Layout title="企业制度">
    <div className="page-header"><div><h1 className="page-title">企业制度</h1>
      <p className="page-desc">查看和下载本企业制度原件，制度管理由企业管理员负责。</p></div></div>
    <UploadedPolicyFiles />
  </Layout>;
  return <PolicyManagement />;
}

function PolicyManagement() {
  const { user } = useAuth();
  const [savedToken] = useState(readRememberedToken);
  const [token, setToken] = useState(savedToken);
  const [connected, setConnected] = useState(false);
  const [accessOpen, setAccessOpen] = useState(false);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [mode, setMode] = useState<PolicyList["embedding_mode"]>("disabled");
  const [items, setItems] = useState<PolicyUpload[]>([]);
  const [selected, setSelected] = useState<PolicyUpload | null>(null);
  const [metadata, setMetadata] = useState<PolicyMetadata>(EMPTY);
  const [checked, setChecked] = useState(false);
  const [remoteConsent, setRemoteConsent] = useState(false);
  const [date, setDate] = useState("2026-09-20");
  const [query, setQuery] = useState("深圳住宿限额是多少？");
  const [result, setResult] = useState<PolicyPreview | null>(null);
  const [busy, setBusy] = useState("");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!savedToken && !ENTERPRISE_MODE) return;
    let active = true;
    setBusy("验证制度访问");
    policyHttp<PolicyList>(savedToken, "/list").then(data => {
      if (!active) return;
      setItems(data.items);
      setMode(data.embedding_mode);
      setConnected(true);
      rememberToken(savedToken);
    }).catch(() => {
      if (!active) return;
      forgetToken();
      setToken("");
      setAccessOpen(!ENTERPRISE_MODE);
      setError(ENTERPRISE_MODE ? '制度访问失败，请检查登录与服务状态。' : "访问验证已失效，请重新验证后上传制度。");
    }).finally(() => { if (active) setBusy(""); });
    return () => { active = false; };
  }, [savedToken]);

  async function refresh() {
    const data = await policyHttp<PolicyList>(token, "/list");
    setItems(data.items);
    setMode(data.embedding_mode);
    return data;
  }
  async function action(label: string, work: () => Promise<void>) {
    setBusy(label);
    setError("");
    setNotice("");
    try {
      await work();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }
  function select(row: PolicyUpload) {
    setSelected(row);
    setMetadata({ ...initialMetadata(row), ...(ENTERPRISE_MODE && user ? { legal_entity: user.company_id } : {}) });
    setChecked(false);
    setResult(null);
    setRemoteConsent(false);
    const m = initialMetadata(row);
    setDate(m.effective_from || (import.meta.env.VITE_M6_DEMO === 'true'
      ? import.meta.env.VITE_M6_BUSINESS_DATE ?? "2026-11-03"
      : new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date())));
    setQuery(m.expense_type === "HOTEL" ? `${m.region || "该地区"}住宿限额是多少？` : "这份制度有哪些适用条件？");
  }
  useEffect(() => {
    setResult(null);
  }, [query, date]);
  async function read(row: PolicyUpload) {
    select(await policyHttp<PolicyUpload>(token, `/${row.upload_id}/read`));
  }
  async function upload(file: File) {
    if (file.size > 10 * 1024 * 1024 || !/\.(pdf|docx)$/i.test(file.name)) {
      setError("仅支持 10 MiB 内的 PDF / DOCX");
      return;
    }
    if (!connected) {
      if (ENTERPRISE_MODE) {
        setError("企业管理员权限尚未验证，请刷新页面或重新登录后上传。");
        return;
      }
      setPendingFile(file);
      setAccessOpen(true);
      setError("");
      setNotice(`已选择「${file.name}」，请完成访问验证后继续上传。`);
      return;
    }
    await action("上传并本地解析", async () => {
      await saveUpload(file);
    });
  }
  async function saveUpload(file: File) {
    setProgress(0);
    const row = await uploadPolicy(token, file, setProgress);
    await refresh();
    await read(row);
    setPendingFile(null);
    setNotice("制度已上传并解析。请对照原文确认制度信息与规则，正式发布后才参与审核。");
  }
  async function connect() {
    await action("验证制度访问", async () => {
      await refresh();
      rememberToken(token);
      setConnected(true);
      setAccessOpen(false);
      if (pendingFile) {
        setBusy("上传并本地解析");
        await saveUpload(pendingFile);
      }
    });
  }
  async function prepareForPublication() {
    if (!selected || mode === "disabled" || (mode === "remote" && !remoteConsent))
      throw new Error("请先启用条款检索；使用远程服务须单独取得明确授权。");
    setBusy("准备发布条款");
    try {
      await policyHttp(token, `/${selected.upload_id}/ingest`, { remote_authorized: remoteConsent });
      await refresh();
      await read(selected);
    } finally { setBusy(""); }
  }
  async function confirm() {
    if (!selected || !checked) {
      setError("请先核对制度原文并勾选人工确认");
      return;
    }
    await action("确认元数据", async () => {
      const row = await policyHttp<PolicyUpload>(
        token,
        `/${selected.upload_id}/confirm`,
        {
          metadata: {
            ...metadata,
            effective_to: metadata.effective_to || null,
          },
          confirmed: true,
        },
      );
      await refresh();
      select(row);
      setNotice("制度信息已确认，接下来请对照原文确认审核规则。");
    });
  }
  async function ingest() {
    if (!selected) return;
    await action("建立草稿索引", async () => {
      await policyHttp(token, `/${selected.upload_id}/ingest`, {
        remote_authorized: remoteConsent,
      });
      await refresh();
      await read(selected);
      setNotice("DRAFT 索引已就绪；正式审批仍使用 ACTIVE 制度。");
    });
    // A failed import state is also read back from the durable database.
    try {
      if (selected) await read(selected);
    } catch {
      /* original action error remains visible */
    }
  }
  async function preview() {
    if (!selected?.metadata) return;
    await action("草稿预览检索", async () => {
      setResult(null);
      const m = selected.metadata!;
      const data = await policyHttp<PolicyPreview>(
        token,
        `/${selected.upload_id}/preview`,
        {
          query,
          legal_entity: m.legal_entity,
          region: m.region,
          expense_type: m.expense_type,
          version: m.version,
          incurred_on: date,
          remote_authorized: remoteConsent,
        },
      );
      setResult(data);
    });
  }
  async function remove() {
    if (!selected) return;
    await action("删除草稿", async () => {
      await policyHttp(token, `/${selected.upload_id}/delete`);
      setSelected(null);
      setResult(null);
      await refresh();
      setNotice("草稿和索引已删除，正式制度保持不变。");
    });
  }
  return (
    <Layout title="企业制度管理">
      <div className="page-header">
        <div>
          <div className="page-title">上传并发布企业制度</div>
          <div className="page-desc">从上传文件提取条款，经人工确认后发布为报销审核依据。</div>
        </div>
      </div>
      {ENTERPRISE_MODE && <UploadedPolicyFiles refreshKey={items.map(row => row.upload_id + row.state).join(',')} />}
      <ol className="policy-workflow" aria-label="制度发布流程">
        <li><span>1</span><div><strong>上传制度</strong><small>解析文件，保留制度原文</small></div></li>
        <li><span>2</span><div><strong>人工确认规则</strong><small>核对适用范围、日期与额度</small></div></li>
        <li><span>3</span><div><strong>正式发布</strong><small>确认版本差异，按生效日期使用</small></div></li>
      </ol>
      <div className="alert">
        未发布的制度仅为草稿，不参与报销审核；已发布制度按费用发生日期确定适用版本。
      </div>
      {error && (
        <div className="alert alert-danger" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="alert alert-success" role="status">
          {notice}
        </div>
      )}
      <section className="card policy-upload-entry" aria-label="上传制度">
        <div className="policy-upload-heading">
          <div><h2>上传制度文件</h2><p>选择企业报销制度，下一步核对原文并确认规则。</p></div>
          <span className={`badge badge-no-dot ${connected ? "badge-tag-green" : "badge-tag-gray"}`}>
            {ENTERPRISE_MODE ? '企业管理员权限' : connected ? "访问已验证" : "待验证访问"}
          </span>
        </div>
        <label className="policy-upload-label">
          选择制度文件
          <input aria-label="选择制度文件" type="file" accept=".pdf,.docx" disabled={!!busy}
            onChange={e => { const file = e.target.files?.[0]; e.target.value = ""; if (file) void upload(file); }} />
        </label>
        <p className="policy-upload-help">支持 PDF / DOCX，文件不超过 10 MB；PDF 最多 3 页。上传后保存为草稿，确认并发布后才参与审核。</p>
        {pendingFile && <p className="policy-pending-file">待上传：{pendingFile.name}
          {connected && <button className="btn" disabled={!!busy} onClick={() => upload(pendingFile)}>重试上传</button>}
        </p>}
        {busy && <p role="status">{busy}…{busy === "上传并本地解析" ? ` ${progress}%（上传完成后继续解析）` : ""}</p>}
        {!ENTERPRISE_MODE && <details className="policy-access-settings" open={accessOpen} onToggle={e => setAccessOpen(e.currentTarget.open)}>
          <summary>访问设置{connected ? " · 已记住凭证" : "（本机演示）"}</summary>
          <div className="policy-access">
            <label>访问凭证<input aria-label="制度访问凭证" type="password" value={token} disabled={!!busy || connected}
              onChange={e => setToken(e.target.value)} autoComplete="off" /></label>
            <button className="btn btn-primary" disabled={!!busy || connected || !token} onClick={connect}>
              {connected ? "访问已验证" : pendingFile ? "验证并上传" : "验证访问"}
            </button>
            {connected && <button className="btn" disabled={!!busy} onClick={() => {
              forgetToken(); setConnected(false); setSelected(null);
              setItems([]); setToken(""); setPendingFile(null);
            }}>断开访问</button>}
            <small>
              仅用于本机演示访问验证。验证通过后凭证会保存在本机浏览器中，下次打开自动恢复；
              点「断开访问」即从本机清除。请使用合成制度文件。
            </small>
          </div>
        </details>}
      </section>
      {connected && (
        <>
          <div className="policy-toolbar">
            <span>制度上传与管理</span>
            <button className="btn" disabled={!!busy} onClick={() => action("刷新制度列表", async () => {
              await refresh(); if (selected) await read(selected);
            })}>刷新列表</button>
          </div>
          <div className="policy-grid">
            <aside className="card policy-list">
              <h2>
                制度草稿 <span>{items.length}</span>
              </h2>
              {!items.length && <p className="page-desc">{ENTERPRISE_MODE
                ? '新增制度可从上方上传；已发布原件可在文件列表下载。'
                : '尚无上传记录，请先选择一份制度文件。'}</p>}
              {items.map((row) => (
                <button
                  key={row.upload_id}
                  disabled={!!busy}
                  className={`policy-list-item${selected?.upload_id === row.upload_id ? " selected" : ""}`}
                  onClick={() => action("读取草稿", () => read(row))}
                >
                  <strong>{row.metadata?.title ?? row.filename}</strong>
                  <span className={`badge badge-no-dot ${STATE_BADGE[row.state]}`}>{STATES[row.state]}</span>
                  <small>
                    {row.metadata
                      ? `${row.metadata.legal_entity} · ${row.metadata.version}`
                      : "元数据待确认"}
                  </small>
                </button>
              ))}
            </aside>
            <div className="policy-detail">
              {selected ? (
                <>
                  <section className="card">
                    <div className="policy-section-head">
                      <div>
                        <div className="publication-eyebrow">第一步</div>
                        <h2>{selected.filename}</h2>
                      </div>
                      <span className={`badge badge-no-dot ${STATE_BADGE[selected.state]}`}>
                        草稿 · {STATES[selected.state]}
                      </span>
                    </div>
                    <details>
                      <summary>查看文件来源与校验信息</summary>
                      <p className="policy-source">
                      {selected.upload_id}
                      <br />
                      来源 {selected.source_uri}
                      <br />
                      SHA-256 {selected.sha256}
                      </p>
                    </details>
                    <p>
                      解析路径：{selected.parsed.methods?.join(" + ")} ·{" "}
                      {selected.parsed.pages
                        ? `${selected.parsed.pages} 页`
                        : "DOCX 文档"}{" "}
                      · {(selected.size / 1024).toFixed(1)} KiB
                    </p>
                    <details open>
                      <summary>提取的制度文字</summary>
                      <pre className="policy-text">{selected.parsed.text}</pre>
                    </details>
                    <div className="policy-actions">
                      <button
                        className="btn"
                        disabled={!!busy}
                        onClick={() =>
                          action("读取原文件", () =>
                            downloadPolicy(token, selected),
                          )
                        }
                      >
                        下载原文件核对
                      </button>
                      <button
                        className="btn btn-danger"
                        disabled={!!busy}
                        onClick={remove}
                      >
                        删除草稿及索引
                      </button>
                    </div>
                  </section>
                  <section className="card">
                    <div className="publication-eyebrow">制度信息</div>
                    <h2>确认制度信息</h2>
                    <p>
                      请对照上传的制度原文核对以下信息。无法可靠识别的内容需人工填写，原始提取结果会保留。
                    </p>
                    {ENTERPRISE_MODE && <p>综合制度可选择全部地区、全部费用类型；具体审核规则的适用范围将在下一步按原文单独核对。</p>}
                    <div className="policy-fields">
                      {LABELS.map(([key, label]) => (
                        <label key={key}>
                          {label}
                          <small>
                            原文：
                            {selected.parsed.extracted_metadata?.[key]?.value ??
                              "待核对"}
                          </small>
                          {ENTERPRISE_MODE && key === "region" ? (
                            <>
                              <select aria-label="地区" disabled={!!busy || !!selected.metadata}
                                value={metadata.region === "*" ? "*" : "SPECIFIC"}
                                onChange={e => {
                                  const original = selected.parsed.extracted_metadata?.region?.value;
                                  setMetadata({ ...metadata, region: e.target.value === "*" ? "*" : original && original !== "UNKNOWN" && original !== "*" ? original : "" });
                                  setChecked(false);
                                }}>
                                <option value="*">全部地区</option><option value="SPECIFIC">指定地区</option>
                              </select>
                              {metadata.region !== "*" && <input aria-label="具体地区" placeholder="填写制度适用地区"
                                value={metadata.region} disabled={!!busy || !!selected.metadata} maxLength={128}
                                onChange={e => { setMetadata({ ...metadata, region: e.target.value }); setChecked(false); }} />}
                            </>
                          ) : key === "expense_type" ? (
                            <select
                              aria-label={label}
                              disabled={!!busy || !!selected.metadata}
                              value={metadata.expense_type}
                              onChange={(e) => {
                                setMetadata({
                                  ...metadata,
                                  expense_type: e.target
                                    .value as PolicyMetadata["expense_type"],
                                });
                                setChecked(false);
                              }}
                            >
                              <option value="">请选择费用类型</option>
                              {ENTERPRISE_MODE && <option value="ALL">全部费用类型</option>}
                              {(["HOTEL", "MEAL", "TRANSPORT", "OTHER"] as const).map(
                                (v) => (
                                  <option key={v} value={v}>
                                    {CATEGORY_LABEL[v] ?? v}
                                  </option>
                                ),
                              )}
                            </select>
                          ) : (
                            <input
                              aria-label={label}
                              disabled={!!busy || !!selected.metadata || (ENTERPRISE_MODE && key === 'legal_entity')}
                              type={
                                key.startsWith("effective_") ? "date" : "text"
                              }
                              placeholder="请对照原文确认"
                              value={metadata[key] ?? ""}
                              maxLength={key === "title" ? 200 : 128}
                              onChange={(e) => {
                                setMetadata({
                                  ...metadata,
                                  [key]: e.target.value,
                                });
                                setChecked(false);
                              }}
                            />
                          )}
                        </label>
                      ))}
                    </div>
                    {!selected.metadata && (
                      <>
                        <label className="policy-check">
                          <input
                            type="checkbox"
                            checked={checked}
                            disabled={!!busy}
                            onChange={(e) => setChecked(e.target.checked)}
                          />
                          我已对照原文核对制度名称、文档适用范围、版本和生效日期
                        </label>
                        <button
                          className="btn btn-primary"
                          disabled={!!busy || !checked}
                          onClick={confirm}
                        >
                          确认制度信息
                        </button>
                      </>
                    )}
                    {selected.metadata && (
                      <p className="policy-confirmed">
                        {ENTERPRISE_MODE ? '制度信息已确认。住宿矩阵与交通、补助标准分别核对发布，发布后按生效日期参与审核。' : '制度信息已确认，文件仍为草稿；请继续确认规则并发布。'}
                      </p>
                    )}
                  </section>
                  {ENTERPRISE_MODE && import.meta.env.VITE_P4A_ENABLED === 'true'
                    && selected.metadata
                    && ['HOTEL', 'ALL'].includes(selected.metadata.expense_type) && (
                    <section className="card">
                      <h2>多职级住宿矩阵</h2>
                      <p>酒店住宿及全部费用类型的综合制度均可在这里继续。请对照原文逐格核对并发布完整住宿矩阵；其他费用标准需分别核对发布。</p>
                      <Link className="btn btn-primary" to={`/hotel-matrix?upload=${encodeURIComponent(selected.upload_id)}`}>
                        确认职级与城市住宿矩阵
                      </Link>
                    </section>
                  )}
                  {ENTERPRISE_MODE ? (selected.metadata ? <TravelPolicyPanel key={selected.upload_id} uploadId={selected.upload_id} /> : null) : <PolicyPublicationPanel key={selected.upload_id} token={token} policy={selected}
                    prepareForPublication={prepareForPublication}
                    canPrepare={mode === "deterministic" || (mode === "remote" && remoteConsent)} />}
                  <details className="card policy-retrieval-settings">
                    <summary>条款检索与原文预览</summary>
                    <div className="publication-eyebrow" style={{ marginTop: 12 }}>辅助核查</div>
                    <p>
                      {mode === "deterministic"
                        ? "当前使用离线测试检索。查看发布差异时会准备已确认制度的条款，尚未接入真实语义模型。"
                        : mode === "remote"
                          ? "远程检索会将制度条款和查询发送到第三方，可能产生费用；仅在独立授权后使用。"
                          : "条款检索暂未开启，已解析的原文和确认信息保留在本机。"}
                    </p>
                    {mode === "remote" && (
                      <label className="policy-check">
                        <input
                          type="checkbox"
                          checked={remoteConsent}
                          disabled={!!busy}
                          onChange={(e) => setRemoteConsent(e.target.checked)}
                        />
                        此文件和查询已获得明确远程发送及付费调用授权
                      </label>
                    )}
                    <button
                      className="btn btn-primary"
                      disabled={
                        !!busy ||
                        !selected.metadata ||
                        mode === "disabled" ||
                        (mode === "remote" && !remoteConsent)
                      }
                      onClick={ingest}
                    >
                      {selected.state === "INDEXED"
                        ? "重新准备条款"
                        : "准备制度条款"}
                    </button>
                    {selected.clauses.length > 0 && (
                      <details>
                        <summary>
                          查看制度条款（{selected.clauses.length} 条）
                        </summary>
                        {selected.clauses.map((c) => (
                          <ClauseCard key={c.chunk_id} clause={c} />
                        ))}
                      </details>
                    )}
                    <div className="policy-scope">
                      预览范围：{selected.policy_version_id}
                      <br />
                      {selected.metadata
                        ? `${selected.metadata.legal_entity} / ${selected.metadata.region === '*' ? '全部地区' : selected.metadata.region} / ${selected.metadata.expense_type === 'ALL' ? '全部费用类型' : CATEGORY_LABEL[selected.metadata.expense_type]} / ${selected.metadata.version}`
                        : "请先确认元数据"}
                    </div>
                    <label className="policy-query">
                      草稿检索问题
                      <textarea
                        aria-label="草稿检索问题"
                        value={query}
                        maxLength={2000}
                        disabled={!!busy}
                        onChange={(e) => setQuery(e.target.value)}
                      />
                    </label>
                    <label className="policy-query">
                      检索业务日期
                      <input
                        aria-label="检索业务日期"
                        type="date"
                        value={date}
                        disabled={!!busy}
                        onChange={(e) => setDate(e.target.value)}
                      />
                    </label>
                    <button
                      className="btn btn-primary"
                      disabled={
                        !!busy ||
                        selected.state !== "INDEXED" ||
                        !query.trim() ||
                        !date ||
                        (mode === "remote" && !remoteConsent)
                      }
                      onClick={preview}
                    >
                      仅检索此草稿制度
                    </button>
                    {result && (
                      <div className="policy-results">
                        <h3>草稿检索结果 · {result.results.length} 条</h3>
                        <p>
                          {result.model} / {result.dimension} 维 ·{" "}
                          {result.policy_version_id}
                        </p>
                        {result.results.map((c) => (
                          <ClauseCard key={c.chunk_id} clause={c} />
                        ))}
                        {!result.results.length && (
                          <p>该草稿没有符合范围的结果。</p>
                        )}
                      </div>
                    )}
                  </details>
                </>
              ) : (
                <section className="card policy-empty">
                  <span>制度草稿</span>
                  <h2>选择一份制度草稿</h2>
                  <p>
                    上传后在这里核对原文、确认制度信息与规则，再正式发布。已发布且生效的版本才参与报销审核。
                  </p>
                </section>
              )}
            </div>
          </div>
        </>
      )}
    </Layout>
  );
}
