import { editGridCell, locateGridCell, summarizeGrid } from './editor-core.mjs';

const $ = selector => document.querySelector(selector);
const fileInput = $('#file');
const dropZone = $('#drop-zone');
const fileLabel = $('#file-label');
const sourcePanel = $('#source-panel');
const sourcePreview = $('#source-preview');
const cutoutButton = $('#cutout');
const cutoutPrompt = $('#cutout-prompt');
const cutoutMessage = $('#cutout-message');
const cutoutResult = $('#cutout-result');
const cutoutPreview = $('#cutout-preview');
const recognitionMode = $('#recognition-mode');
const localGenerateButton = $('#generate-local');
const image2GenerateButton = $('#generate-image2');
const message = $('#message');
const clearCacheButton = $('#clear-cache');
const settingsDialog = $('#api-settings-dialog');
const settingsMessage = $('#api-settings-message');
const canvas = $('#pattern-editor');
const context = canvas.getContext('2d');

let sourceBlob = null;
let workingBlob = null;
let proposedCutout = null;
let sourceUrl = null;
let cutoutUrl = null;
let patternGrid = [];
let availableColours = [];
let colourByCode = new Map();
let selectedCell = null;
let editHistory = [];
let editorCellSize = 24;

const BOARD_FALLBACK = [
  { id: '52x52', label: '52 钉单板', width: 52, height: 52 },
  { id: '72x72', label: '72 钉单板', width: 72, height: 72 },
  { id: '78x78', label: '78 钉单板', width: 78, height: 78 },
  { id: '104x104', label: '104 钉单板', width: 104, height: 104 },
];

async function loadVersion() {
  try {
    const response = await fetch('/api/version', { cache: 'no-store' });
    if (!response.ok) throw new Error();
    const data = await response.json();
    $('#app-version').textContent = data.version;
    const releaseTitle = $('#release-title');
    const versionChanges = $('#version-changes');
    if (releaseTitle) releaseTitle.textContent = `v${data.version} 更新内容`;
    if (versionChanges && Array.isArray(data.changes)) versionChanges.innerHTML = data.changes.map(change => `<li>${change}</li>`).join('');
  } catch (_) {
    // HTML 内保留版本信息，旧后端或临时网络异常时仍然可见。
  }
}

async function loadConfig() {
  try {
    const response = await fetch('/api/config', { cache: 'no-store' });
    if (!response.ok) throw new Error();
    const data = await response.json();
    const image2Option = recognitionMode.querySelector('option[value="image2"]');
    const containerOption = recognitionMode.querySelector('option[value="container"]');
    if (containerOption && data.local_cutout_enabled) containerOption.textContent = '本地抠图';
    if (data.ai_enabled) {
      image2Option.disabled = false;
      image2Option.textContent = 'AI 抠图';
      $('#ai-status').textContent = '本地抠图不发送图片、不耗 Token；AI 抠图会发送图片到已配置的兼容 API，并可能产生费用。';
    } else {
      image2Option.disabled = true;
      image2Option.textContent = 'AI 抠图（未配置）';
      if (recognitionMode.value === 'image2') recognitionMode.value = 'container';
      $('#ai-status').textContent = '本地抠图不发送图片、不耗 Token。配置 API 后可使用 AI 抠图。';
    }
  } catch (_) {
    $('#ai-status').textContent = '无法读取 AI 配置，本地抠图仍可正常使用。';
  }
}

function settingsPassword() {
  return $('#settings-password').value;
}

function settingsPayload() {
  const key = $('#api-key').value.trim();
  return {
    api_url: $('#api-url').value.trim(),
    model: $('#api-model').value.trim(),
    vision_model: $('#vision-model').value.trim(),
    quality: $('#api-quality').value,
    ...(key ? { api_key: key } : {}),
  };
}

function apiErrorMessage(data, fallback) {
  if (typeof data?.detail === 'string') return data.detail;
  if (Array.isArray(data?.detail)) return data.detail.map(item => item.msg).join('；');
  return fallback;
}

async function settingsRequest(path, options = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(path, {
      cache: 'no-store',
      ...options,
      signal: options.signal || controller.signal,
      headers: {
        'X-Settings-Password': settingsPassword(),
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.headers || {}),
      },
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* 使用下面的通用错误。 */ }
    if (!response.ok) throw new Error(apiErrorMessage(data, `请求失败（${response.status}）`));
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超过 30 秒，请检查容器状态后重试。');
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function recommendedImageEditModel(models) {
  const priorities = ['gpt-image-2', 'gpt-image-1.5', 'gpt-image-1', 'gpt-image-1-mini'];
  return priorities.find(name => models.includes(name)) || null;
}

function fillModelChoices(models) {
  const datalist = $('#api-model-list');
  datalist.replaceChildren();
  models.forEach(model => {
    const option = document.createElement('option');
    option.value = model;
    datalist.append(option);
  });
}

function fillAPISettings(data) {
  $('#api-url').value = data.api_url;
  $('#api-model').value = data.model;
  $('#vision-model').value = data.vision_model || 'gpt-5.5';
  $('#api-quality').value = data.quality;
  $('#api-key').value = '';
  $('#api-key-state').textContent = data.has_api_key
    ? '已保存密钥；输入新值可替换，留空会保留。'
    : '当前未保存 API Key。';
}

async function loadAPISettings() {
  if (!settingsPassword()) throw new Error('请先输入设置管理密码。');
  const data = await settingsRequest('/api/settings');
  fillAPISettings(data);
  return data;
}

$('#open-api-settings').addEventListener('click', () => {
  settingsMessage.textContent = '输入管理密码后，可读取、测试或保存接口设置。';
  if (typeof settingsDialog.showModal === 'function') settingsDialog.showModal();
  else settingsDialog.setAttribute('open', '');
  $('#settings-password').focus();
});

$('#close-api-settings').addEventListener('click', () => settingsDialog.close());
settingsDialog.addEventListener('click', event => {
  if (event.target === settingsDialog) settingsDialog.close();
});

$('#load-api-settings').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  settingsMessage.textContent = '正在读取…';
  try {
    await loadAPISettings();
    settingsMessage.textContent = '已读取当前设置；API Key 不会显示。';
  } catch (error) {
    settingsMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$('#test-api-settings').addEventListener('click', async event => {
  if (!settingsPassword()) { settingsMessage.textContent = '请先输入设置管理密码。'; return; }
  const button = event.currentTarget;
  button.disabled = true;
  settingsMessage.textContent = '正在请求兼容接口的 /models…';
  try {
    const data = await settingsRequest('/api/settings/test', {
      method: 'POST', body: JSON.stringify(settingsPayload()),
    });
    const models = Array.isArray(data.models) ? data.models : [];
    fillModelChoices(models);
    const recommended = recommendedImageEditModel(models);
    settingsMessage.textContent = recommended
      ? `${data.message}；当前图像编辑建议使用 ${recommended}。`
      : `${data.message}；未找到已知的 GPT Image 编辑模型。`;
  } catch (error) {
    settingsMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$('#save-api-settings').addEventListener('click', async event => {
  if (!settingsPassword()) { settingsMessage.textContent = '请先输入设置管理密码。'; return; }
  const button = event.currentTarget;
  button.disabled = true;
  settingsMessage.textContent = '正在保存…';
  try {
    const data = await settingsRequest('/api/settings', {
      method: 'PUT', body: JSON.stringify(settingsPayload()),
    });
    fillAPISettings(data);
    settingsMessage.textContent = 'API 设置已保存并从磁盘重新读取校验，可以继续修改后再次保存。';
    void loadConfig();
  } catch (error) {
    settingsMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

['#api-url', '#api-key', '#api-model', '#vision-model', '#api-quality'].forEach(selector => {
  const field = $(selector);
  const eventName = field.tagName === 'SELECT' ? 'change' : 'input';
  field.addEventListener(eventName, () => {
    $('#save-api-settings').disabled = false;
    settingsMessage.textContent = '设置已修改，点击“保存”应用新值。';
  });
});

$('#clear-api-key').addEventListener('click', async event => {
  if (!settingsPassword()) { settingsMessage.textContent = '请先输入设置管理密码。'; return; }
  if (!window.confirm('确定删除容器中保存的 API Key 吗？云端识图会立即停用。')) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const data = await settingsRequest('/api/settings/key', { method: 'DELETE' });
    fillAPISettings(data);
    settingsMessage.textContent = data.message;
    await loadConfig();
  } catch (error) {
    settingsMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

clearCacheButton.addEventListener('click', async () => {
  clearCacheButton.disabled = true;
  clearCacheButton.textContent = '正在清除…';
  try {
    await fetch('/api/cache/clear', { method: 'POST', cache: 'no-store' });
    if ('caches' in window) {
      const names = await caches.keys();
      await Promise.all(names.map(name => caches.delete(name)));
    }
  } finally {
    location.replace(`${location.pathname}?refresh=${Date.now()}`);
  }
});

async function loadBoards() {
  try {
    const response = await fetch('/api/boards');
    if (!response.ok) throw new Error();
    fillBoards(await response.json());
  } catch (_) {
    fillBoards(BOARD_FALLBACK);
  }
}

function fillBoards(boards) {
  const boardInput = $('#board');
  const options = $('#board-options');
  const selected = boards.some(item => item.id === boardInput.value) ? boardInput.value : boards[0]?.id;
  boardInput.value = selected || '';
  options.replaceChildren();

  boards.forEach(item => {
    const button = document.createElement('button');
    const size = document.createElement('strong');
    const label = document.createElement('span');
    const meta = document.createElement('small');
    button.type = 'button';
    button.className = 'board-choice';
    button.dataset.board = item.id;
    button.setAttribute('role', 'radio');
    size.textContent = (item.width || item.id.split('x')[0]) + ' × ' + (item.height || item.id.split('x')[1]);
    label.textContent = item.label;
    meta.textContent = '钉位';
    button.append(size, label, meta);
    button.addEventListener('click', () => {
      boardInput.value = item.id;
      options.querySelectorAll('.board-choice').forEach(choice => {
        const isSelected = choice.dataset.board === item.id;
        choice.classList.toggle('selected', isSelected);
        choice.setAttribute('aria-checked', String(isSelected));
      });
    });
    const isSelected = item.id === selected;
    button.classList.toggle('selected', isSelected);
    button.setAttribute('aria-checked', String(isSelected));
    options.append(button);
  });
}

function clearObjectUrl(url) {
  if (url) URL.revokeObjectURL(url);
}

function selectFile(file) {
  if (!file || !file.type.startsWith('image/')) {
    message.textContent = '请选择 PNG、JPG 或 WebP 图片。';
    return;
  }
  if (file.size > 12 * 1024 * 1024) {
    message.textContent = '图片不能超过 12 MB。';
    return;
  }
  sourceBlob = file;
  workingBlob = file;
  proposedCutout = null;
  clearObjectUrl(sourceUrl);
  sourceUrl = URL.createObjectURL(file);
  sourcePreview.src = sourceUrl;
  fileLabel.textContent = file.name || '已选择图片';
  sourcePanel.classList.remove('hidden');
  cutoutResult.classList.add('hidden');
  $('#result').classList.add('hidden');
  cutoutMessage.textContent = '';
  message.textContent = '已使用原图，可直接生成，也可以先抠图。';
}

fileInput.addEventListener('change', () => selectFile(fileInput.files[0]));
['dragenter', 'dragover'].forEach(name => dropZone.addEventListener(name, event => {
  event.preventDefault();
  dropZone.classList.add('drag');
}));
['dragleave', 'drop'].forEach(name => dropZone.addEventListener(name, event => {
  event.preventDefault();
  dropZone.classList.remove('drag');
}));
dropZone.addEventListener('drop', event => selectFile(event.dataTransfer.files[0]));

recognitionMode.addEventListener('change', () => {
  $('#ai-status').textContent = recognitionMode.value === 'image2'
    ? 'AI 抠图会发送图片到已配置的兼容 API，并可能产生费用。'
    : '本地抠图在容器内运行，图片不会发送到第三方。';
});

async function dataUrlToBlob(dataUrl) {
  const response = await fetch(dataUrl);
  return response.blob();
}

function showCutoutResult(blob, description) {
  proposedCutout = blob;
  clearObjectUrl(cutoutUrl);
  cutoutUrl = URL.createObjectURL(blob);
  cutoutPreview.src = cutoutUrl;
  cutoutResult.classList.remove('hidden');
  cutoutMessage.textContent = description;
  cutoutResult.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function runImage2PatternReference() {
  const input = workingBlob || sourceBlob;
  const form = new FormData();
  form.append('image', input, input.type === 'image/png' ? 'image.png' : 'image.jpg');
  const response = await fetch('/api/ai/pattern-reference', { method: 'POST', body: form });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Image2 色块优化失败');
  showCutoutResult(await dataUrlToBlob(data.image), (data.model || 'Image2') + ' 色块优化完成，请确认结果。');
}

async function runContainerCutout() {
  const form = new FormData();
  form.append('image', sourceBlob, sourceBlob.type === 'image/png' ? 'image.png' : 'image.jpg');
  const response = await fetch('/api/local-cutout', { method: 'POST', body: form });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '容器本地抠图失败');
  showCutoutResult(await dataUrlToBlob(data.image), (data.engine || '容器本地分割') + ' 完成，请确认结果。');
}

cutoutButton.addEventListener('click', async () => {
  if (!sourceBlob) { cutoutMessage.textContent = '请先选择一张图片。'; return; }
  cutoutButton.disabled = true;
  cutoutMessage.textContent = recognitionMode.value === 'image2'
    ? 'AI 正在处理图片，可能产生费用…'
    : '容器正在运行本地抠图模型，首次处理可能需要十几秒…';
  try {
    if (recognitionMode.value === 'image2') await runImage2PatternReference();
    else await runContainerCutout();
  } catch (error) {
    cutoutMessage.textContent = '处理失败：' + (error.message || '请检查网络后重试');
  } finally {
    cutoutButton.disabled = false;
  }
});

$('#use-cutout').addEventListener('click', () => {
  if (!proposedCutout) return;
  workingBlob = proposedCutout;
  cutoutResult.classList.add('hidden');
  cutoutMessage.textContent = '已采用处理结果，透明区域不会放豆。';
  message.textContent = '已采用处理结果，可以生成图纸。';
});

$('#discard-cutout').addEventListener('click', () => {
  workingBlob = sourceBlob;
  proposedCutout = null;
  cutoutResult.classList.add('hidden');
  cutoutMessage.textContent = '已弃用处理结果，继续使用原图。';
  message.textContent = '正在使用原图。';
});

function textColour(rgb) {
  const luminance = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
  return luminance > 145 ? '#111111' : '#ffffff';
}

function drawPattern() {
  if (!patternGrid.length) return;
  const rows = patternGrid.length;
  const columns = patternGrid[0].length;
  editorCellSize = Math.max(20, Math.min(36, Math.floor(1800 / columns)));
  const margin = 2;
  canvas.width = columns * editorCellSize + margin * 2;
  canvas.height = rows * editorCellSize + margin * 2;
  context.fillStyle = '#ffffff';
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  context.font = `700 ${Math.max(9, Math.floor(editorCellSize / 3))}px Arial, sans-serif`;

  patternGrid.forEach((row, rowIndex) => row.forEach((code, columnIndex) => {
    const x = margin + columnIndex * editorCellSize;
    const y = margin + rowIndex * editorCellSize;
    const colour = code ? colourByCode.get(code) : null;
    context.fillStyle = colour ? `rgb(${colour.rgb.join(',')})` : '#fafafa';
    context.fillRect(x, y, editorCellSize, editorCellSize);
    context.strokeStyle = '#77716a';
    context.lineWidth = 1;
    context.strokeRect(x + 0.5, y + 0.5, editorCellSize - 1, editorCellSize - 1);
    if (colour) {
      context.fillStyle = textColour(colour.rgb);
      context.fillText(code, x + editorCellSize / 2, y + editorCellSize / 2);
    }
  }));

  if (selectedCell) {
    const x = margin + selectedCell.column * editorCellSize;
    const y = margin + selectedCell.row * editorCellSize;
    context.strokeStyle = '#ffcf33';
    context.lineWidth = 4;
    context.strokeRect(x + 2, y + 2, editorCellSize - 4, editorCellSize - 4);
  }
  $('#download').href = canvas.toDataURL('image/png');
}

function renderMaterials(items, total, board, empty = 0) {
  const width = board?.width || board?.columns;
  const height = board?.height || board?.rows;
  $('#total').textContent = width && height
    ? `${width} × ${height} · ${total} 颗${empty ? ` · ${empty} 格留空` : ''}`
    : `${total} 颗`;
  $('#palette').innerHTML = items.map(item => `
    <div class="swatch-row">
      <span class="swatch" style="background:${item.rgb ? `rgb(${item.rgb.join(',')})` : item.hex}"></span>
      <span><strong>${item.code}</strong><small>MARD 2.6 mm</small></span>
      <strong>${item.count} 颗</strong>
    </div>`).join('');
}

function refreshMaterials() {
  const { counts, total, rows, columns, empty } = summarizeGrid(patternGrid);
  const items = [...counts.entries()]
    .map(([code, count]) => ({ ...colourByCode.get(code), count }))
    .sort((a, b) => b.count - a.count);
  renderMaterials(items, total, { width: columns, height: rows }, empty);
}

function updatePixelPanel() {
  if (!selectedCell) {
    $('#pixel-editor').classList.add('hidden');
    return;
  }
  const code = patternGrid[selectedCell.row][selectedCell.column];
  const colour = code ? colourByCode.get(code) : null;
  $('#pixel-position').textContent = `第 ${selectedCell.row + 1} 行 · 第 ${selectedCell.column + 1} 列`;
  $('#pixel-current').textContent = colour ? `当前：${code}` : '当前：空白';
  $('#pixel-preview').style.background = colour ? `rgb(${colour.rgb.join(',')})` : 'transparent';
  $('#pixel-colour').value = code || '';
  $('#pixel-editor').classList.remove('hidden');
}

function initializeEditor(data) {
  patternGrid = data.grid.map(row => [...row]);
  availableColours = data.colours;
  colourByCode = new Map(availableColours.map(item => [item.code, item]));
  selectedCell = null;
  updatePixelPanel();
  editHistory = [];
  $('#pixel-colour').innerHTML = [
    '<option value="">空白（不放豆）</option>',
    ...availableColours.map(item => `<option value="${item.code}">${item.code} · ${item.name}</option>`),
  ].join('');
  $('#pattern').classList.add('hidden');
  canvas.classList.remove('hidden');
  drawPattern();
  refreshMaterials();
}

canvas.addEventListener('click', event => {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  const { row, column } = locateGridCell({
    clientX: event.clientX,
    clientY: event.clientY,
    left: rect.left,
    top: rect.top,
    scaleX,
    scaleY,
    margin: 2,
    cellSize: editorCellSize,
  });
  if (row < 0 || column < 0 || row >= patternGrid.length || column >= patternGrid[0].length) return;
  selectedCell = { row, column };
  drawPattern();
  updatePixelPanel();
});

function setSelectedPixel(code) {
  if (!selectedCell) return;
  const oldCode = patternGrid[selectedCell.row][selectedCell.column];
  if (oldCode === code) return;
  editHistory.push({ ...selectedCell, oldCode, newCode: code });
  editGridCell(patternGrid, selectedCell.row, selectedCell.column, code);
  drawPattern();
  refreshMaterials();
  updatePixelPanel();
}

$('#apply-pixel').addEventListener('click', () => setSelectedPixel($('#pixel-colour').value || null));
$('#clear-pixel').addEventListener('click', () => setSelectedPixel(null));
$('#undo-pixel').addEventListener('click', () => {
  const edit = editHistory.pop();
  if (!edit) return;
  editGridCell(patternGrid, edit.row, edit.column, edit.oldCode);
  selectedCell = { row: edit.row, column: edit.column };
  drawPattern();
  refreshMaterials();
  updatePixelPanel();
});

function loadImage(dataUrl) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('无法读取 Image2 图纸'));
    image.src = dataUrl;
  });
}

function closestMardCode(rgb) {
  let closest = null;
  let distance = Number.POSITIVE_INFINITY;
  colourByCode.forEach(colour => {
    const score = (rgb[0] - colour.rgb[0]) ** 2 + (rgb[1] - colour.rgb[1]) ** 2 + (rgb[2] - colour.rgb[2]) ** 2;
    if (score < distance) {
      closest = colour.code;
      distance = score;
    }
  });
  return closest;
}

function clearExteriorWhite(grid) {
  const rows = grid.length;
  const columns = grid[0]?.length || 0;
  const isWhite = code => {
    const rgb = colourByCode.get(code)?.rgb;
    return rgb && rgb[0] > 238 && rgb[1] > 238 && rgb[2] > 238;
  };
  const queue = [];
  const enqueue = (row, column) => {
    if (row < 0 || column < 0 || row >= rows || column >= columns || !isWhite(grid[row][column])) return;
    grid[row][column] = null;
    queue.push([row, column]);
  };
  for (let column = 0; column < columns; column += 1) { enqueue(0, column); enqueue(rows - 1, column); }
  for (let row = 0; row < rows; row += 1) { enqueue(row, 0); enqueue(row, columns - 1); }
  while (queue.length) {
    const [row, column] = queue.shift();
    enqueue(row - 1, column); enqueue(row + 1, column); enqueue(row, column - 1); enqueue(row, column + 1);
  }
}

async function image2Grid(data) {
  const board = data.board;
  const image = await loadImage(data.image);
  const sampler = document.createElement('canvas');
  sampler.width = image.naturalWidth;
  sampler.height = image.naturalHeight;
  const samplerContext = sampler.getContext('2d', { willReadFrequently: true });
  samplerContext.drawImage(image, 0, 0);
  const pixels = samplerContext.getImageData(0, 0, sampler.width, sampler.height).data;
  const offsets = [0.18, 0.32, 0.68, 0.82];
  const codeAt = (row, column) => {
    const votes = new Map();
    offsets.forEach(yOffset => offsets.forEach(xOffset => {
      const x = Math.min(sampler.width - 1, Math.floor((column + xOffset) * sampler.width / board.width));
      const y = Math.min(sampler.height - 1, Math.floor((row + yOffset) * sampler.height / board.height));
      const index = (y * sampler.width + x) * 4;
      if (pixels[index + 3] < 128) return;
      const code = closestMardCode([pixels[index], pixels[index + 1], pixels[index + 2]]);
      votes.set(code, (votes.get(code) || 0) + 1);
    }));
    return [...votes.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || null;
  };
  const grid = Array.from({ length: board.height }, (_, row) => Array.from({ length: board.width }, (_, column) => codeAt(row, column)));
  clearExteriorWhite(grid);
  return grid;
}

async function showDirectImage2Pattern(data) {
  availableColours = data.colours || [];
  colourByCode = new Map(availableColours.map(item => [item.code, item]));
  if (!colourByCode.size) throw new Error('Image2 图纸缺少 MARD 色卡数据，无法进入编辑器');
  const grid = await image2Grid(data);
  initializeEditor({ grid, colours: availableColours });
  if (data.material_warning) $('#palette').insertAdjacentHTML('afterbegin', `<p class="status">${data.material_warning}</p>`);
}

async function generatePattern(mode) {
  if (!workingBlob) { message.textContent = '请先上传一张图片。'; return; }
  const form = new FormData();
  form.append('image', workingBlob, workingBlob.type === 'image/png' ? 'subject.png' : 'subject.jpg');
  form.append('board', $('#board').value);
  const isImage2 = mode === 'image2';
  localGenerateButton.disabled = true;
  image2GenerateButton.disabled = true;
  message.textContent = isImage2
    ? '正在交由 Image2 识图生成拼豆图纸，可能产生费用…'
    : '正在使用本地算法生成拼豆图纸…';
  try {
    const response = await fetch(isImage2 ? '/api/ai/generate-pattern' : '/api/generate', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || (isImage2 ? 'Image2 图纸生成失败' : '本地图纸生成失败'));
    if (isImage2) await showDirectImage2Pattern(data);
    else initializeEditor(data);
    $('#result').classList.remove('hidden');
    message.textContent = isImage2
      ? (data.engine || 'Image2') + ' 图纸已解析为可编辑色块，可点击任意格子修正色号。'
      : '本地图纸已生成，可点击格子手动修正色号。';
    $('#result').scrollIntoView({ behavior: 'smooth' });
  } catch (error) {
    message.textContent = error.message;
  } finally {
    localGenerateButton.disabled = false;
    image2GenerateButton.disabled = false;
  }
}

localGenerateButton.addEventListener('click', () => generatePattern('local'));
image2GenerateButton.addEventListener('click', () => generatePattern('image2'));

loadVersion();
loadConfig();
loadBoards();




