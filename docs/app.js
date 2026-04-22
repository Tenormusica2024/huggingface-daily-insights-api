const REPO = "Tenormusica2024/huggingface-daily-insights-api";
const RELEASE_API = `https://api.github.com/repos/${REPO}/releases/latest`;
const FILES = ["models.csv", "model_snapshots.csv", "papers.csv", "arena_rankings.csv"];

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("en-US");

function setStatus(text, state = "loading") {
  $("status-text").textContent = text;
  const dot = $("status-dot");
  dot.className = "status-dot";
  if (state === "ok") dot.classList.add("ok");
  if (state === "error") dot.classList.add("error");
}

function csvParse(text) {
  const rows = [];
  let row = [];
  let value = "";
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    const next = text[i + 1];
    if (inQuotes) {
      if (char === '"' && next === '"') {
        value += '"';
        i += 1;
      } else if (char === '"') {
        inQuotes = false;
      } else {
        value += char;
      }
    } else if (char === '"') {
      inQuotes = true;
    } else if (char === ",") {
      row.push(value);
      value = "";
    } else if (char === "\n") {
      row.push(value);
      rows.push(row);
      row = [];
      value = "";
    } else if (char !== "\r") {
      value += char;
    }
  }
  if (value.length || row.length) {
    row.push(value);
    rows.push(row);
  }
  if (!rows.length) return [];
  const headers = rows.shift();
  return rows
    .filter((items) => items.some((item) => item !== ""))
    .map((items) => Object.fromEntries(headers.map((header, index) => [header, items[index] ?? ""])));
}

async function fetchText(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.text();
}

function assetMap(release) {
  return Object.fromEntries((release.assets || []).map((asset) => [asset.name, asset.browser_download_url]));
}

function renderAssetLinks(assets) {
  const wrap = $("asset-links");
  wrap.innerHTML = "";
  FILES.forEach((name) => {
    const link = document.createElement("a");
    link.href = assets[name] || `https://github.com/${REPO}/releases/latest`;
    link.textContent = name;
    wrap.appendChild(link);
  });
}

function renderMetrics(data) {
  $("models-count").textContent = number.format(data.models.length);
  $("snapshots-count").textContent = number.format(data.snapshots.length);
  $("papers-count").textContent = number.format(data.papers.length);
  $("arena-count").textContent = number.format(data.arena.length);
}

function renderTrending(snapshots) {
  const byModel = new Map();
  snapshots.forEach((row) => {
    if (!row.model_id || !row.snapshot_date) return;
    if (!byModel.has(row.model_id)) byModel.set(row.model_id, []);
    byModel.get(row.model_id).push(row);
  });

  const scored = [];
  const dates = new Set();
  byModel.forEach((rows, modelId) => {
    rows.sort((a, b) => a.snapshot_date.localeCompare(b.snapshot_date));
    if (rows.length < 2) return;
    const first = rows[0];
    const latest = rows[rows.length - 1];
    const firstLikes = Number(first.likes || 0);
    const latestLikes = Number(latest.likes || 0);
    dates.add(first.snapshot_date);
    dates.add(latest.snapshot_date);
    scored.push({
      modelId,
      pipeline: latest.pipeline_tag || "—",
      delta: latestLikes - firstLikes,
      latest: latestLikes,
    });
  });

  scored.sort((a, b) => b.delta - a.delta || b.latest - a.latest);
  const body = $("trending-body");
  body.innerHTML = "";
  scored.slice(0, 12).forEach((item) => {
    const tr = document.createElement("tr");
    const href = `https://huggingface.co/${item.modelId}`;
    tr.innerHTML = `
      <td><a class="model-link" href="${href}">${escapeHtml(item.modelId)}</a></td>
      <td>${escapeHtml(item.pipeline)}</td>
      <td class="num">${number.format(item.delta)}</td>
      <td class="num">${number.format(item.latest)}</td>
    `;
    body.appendChild(tr);
  });
  if (!scored.length) body.innerHTML = '<tr><td colspan="4">No snapshot deltas available.</td></tr>';

  const sortedDates = [...dates].sort();
  $("snapshot-range").textContent = sortedDates.length ? `${sortedDates[0]} → ${sortedDates[sortedDates.length - 1]}` : "—";
}

function renderPapers(papers) {
  const list = $("papers-list");
  list.innerHTML = "";
  papers
    .filter((paper) => paper.arxiv_id && paper.title)
    .sort((a, b) => String(b.submitted_at).localeCompare(String(a.submitted_at)))
    .slice(0, 8)
    .forEach((paper) => {
      const li = document.createElement("li");
      li.innerHTML = `
        <a class="paper-title" href="https://arxiv.org/abs/${escapeHtml(paper.arxiv_id)}">${escapeHtml(paper.title)}</a>
        <span class="paper-meta">${escapeHtml(paper.category || "—")} · ${escapeHtml((paper.submitted_at || "").slice(0, 10))}</span>
      `;
      list.appendChild(li);
    });
  if (!list.children.length) list.innerHTML = "<li>No recent paper data available.</li>";
}

function renderArena(arena) {
  const dates = [...new Set(arena.map((row) => row.snapshot_date).filter(Boolean))].sort();
  const latestDate = dates[dates.length - 1];
  $("arena-date").textContent = latestDate || "—";
  const body = $("arena-body");
  body.innerHTML = "";
  arena
    .filter((row) => row.snapshot_date === latestDate)
    .sort((a, b) => Number(a.rank) - Number(b.rank))
    .slice(0, 12)
    .forEach((row) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="num">${number.format(Number(row.rank))}</td>
        <td>${escapeHtml(row.model_name || "—")}</td>
        <td class="num">${number.format(Number(row.elo_score || 0))}</td>
      `;
      body.appendChild(tr);
    });
  if (!body.children.length) body.innerHTML = '<tr><td colspan="3">No arena data available.</td></tr>';
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char]));
}

async function main() {
  try {
    const release = await fetch(RELEASE_API).then((res) => {
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json();
    });
    $("latest-release-link").href = release.html_url;
    setStatus(`Loaded ${release.tag_name}`, "ok");
    const assets = assetMap(release);
    renderAssetLinks(assets);

    const [models, snapshots, papers, arena] = await Promise.all(
      FILES.map(async (name) => csvParse(await fetchText(assets[name])))
    );
    const data = { models, snapshots, papers, arena };
    renderMetrics(data);
    renderTrending(snapshots);
    renderPapers(papers);
    renderArena(arena);
  } catch (error) {
    console.error(error);
    setStatus(`Unable to load release data: ${error.message}`, "error");
    renderAssetLinks({});
  }
}

main();
