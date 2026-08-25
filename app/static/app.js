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
const aiSubjectPanel = $('#ai-subject-panel');
const aiSubjectOptions = $('#ai-subject-options');
const applyAiCutoutButton = $('#apply-ai-cutout');
const cutoutResult = $('#cutout-result');
const cutoutPreview = $('#cutout-preview');
const recognitionMode = $('#recognition-mode');
const generateButton = $('#generate');
const message = $('#message');
const clearCacheButton = $('#clear-cache');
const settingsDialog = $('#api-settings-dialog');
const settingsMessage = $('#api-settings-message');
const canvas = $('#pattern-editor');
const context = canvas.getContext('2d');

let sourceBlob = null;
let workingBlob = null;
let proposedCutout = null;
let selectedAISubject = null;
let sourceUrl = null;
let cutoutUrl = null;
let clipsegPromise = null;
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
    const option = recognitionMode.querySelector('option[value="openai"]');
    const image2Option = recognitionMode.querySelector('option[value="image2"]');
    const containerOption = recognitionMode.querySelector('option[value="container"]');
    if (containerOption && data.local_cutout_enabled) {
      containerOption.textContent = '容器本地抠图（' + (data.local_cutout_model || '离线分割模型') + ' · 不耗 Token）';
    }
    if (data.ai_enabled) {
      option.disabled = false;
      image2Option.disabled = false;
      image2Option.textContent = (data.ai_model || 'Image2') + ' 色块优化（消耗 Token）';
      option.textContent = (data.vision_model || '识图模型') + ' 识图 + ' + data.ai_model + ' 抠图';
      $('#ai-status').textContent = '推荐使用容器本地抠图：不发送图片、不耗 Token；云端模式先分析主体选项，再由图像模型执行抠图，可能产生费用。';
    } else {
      option.disabled = true;
      image2Option.disabled = true;
      image2Option.textContent = 'Image2 色块优化（未配置）';
      option.textContent = 'OpenAI 兼容 API（未配置）';
      if (recognitionMode.value === 'openai' || recognitionMode.value === 'image2') recognitionMode.value = 'container';
      $('#ai-status').textContent = '推荐使用容器本地抠图：不发送图片、不耗 Token。浏览器文字识图和云端模式可按需要选择。';
    }
  } catch (_) {
    $('#ai-status').textContent = '无法读取 AI 配置，本地识图仍可正常使用。';
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
  event.currentTarget.disabled = true;
  settingsMessage.textContent = '正在读取…';
  try {
    await loadAPISettings();
    settingsMessage.textContent = '已读取当前设置；API Key 不会显示。';
  } catch (error) {
    settingsMessage.textContent = error.message;
  } finally {
    event.currentTarget.disabled = false;
  }
});

$('#test-api-settings').addEventListener('click', async event => {
  if (!settingsPassword()) { settingsMessage.textContent = '请先输入设置管理密码。'; return; }
  event.currentTarget.disabled = true;
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
    event.currentTarget.disabled = false;
  }
});

$('#save-api-settings').addEventListener('click', async event => {
  if (!settingsPassword()) { settingsMessage.textContent = '请先输入设置管理密码。'; return; }
  event.currentTarget.disabled = true;
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
    event.currentTarget.disabled = false;
  }
});

['#api-url', '#api-key', '#api-model', '#api-quality'].forEach(selector => {
  const field = $(selector);
  const eventName = field.tagName === 'SELECT' ? 'change' : 'input';
  field.addEventListener(eventName, () => {
    $('#save-api-settings').disabled = false;
    settingsMessage.textContent = '设置已修改，点击“保存设置”应用新值。';
  });
});

$('#clear-api-key').addEventListener('click', async event => {
  if (!settingsPassword()) { settingsMessage.textContent = '请先输入设置管理密码。'; return; }
  if (!window.confirm('确定删除容器中保存的 API Key 吗？云端识图会立即停用。')) return;
  event.currentTarget.disabled = true;
  try {
    const data = await settingsRequest('/api/settings/key', { method: 'DELETE' });
    fillAPISettings(data);
    settingsMessage.textContent = data.message;
    await loadConfig();
  } catch (error) {
    settingsMessage.textContent = error.message;
  } finally {
    event.currentTarget.disabled = false;
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
  $('#ai-status').textContent = recognitionMode.value === 'openai'
    ? '当前图片将在点击处理后发送到已配置的兼容 API，并可能产生费用。'
    : recognitionMode.value === 'image2'
      ? 'Image2 会生成色块更清晰的参考图；请预览确认后再用本地 MARD 算法生成图纸，可能产生费用。'
      : '本地模式在浏览器内运行，图片不会发送到第三方。';
});

function promptForModel(text) {
  const replacements = [
    [/人物|人像|人/g, 'person'], [/白色的猫|白猫/g, 'white cat'], [/猫/g, 'cat'], [/狗/g, 'dog'],
    [/红色汽车|红色的车/g, 'red car'], [/汽车|车辆|车/g, 'car'], [/花朵|花/g, 'flower'],
    [/鸟/g, 'bird'], [/建筑|房子/g, 'building'], [/食物/g, 'food'], [/玩具/g, 'toy'],
  ];
  let translated = text.trim();
  replacements.forEach(([pattern, value]) => { translated = translated.replace(pattern, value); });
  return translated;
}

async function getClipseg() {
  if (!clipsegPromise) {
    clipsegPromise = (async () => {
      const module = await import('https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.2/+esm');
      const modelId = 'Xenova/clipseg-rd64-refined';
      const [tokenizer, processor, model] = await Promise.all([
        module.AutoTokenizer.from_pretrained(modelId),
        module.AutoProcessor.from_pretrained(modelId),
        module.CLIPSegForImageSegmentation.from_pretrained(modelId, { dtype: 'q8' }),
      ]);
      return { ...module, tokenizer, processor, model };
    })();
  }
  return clipsegPromise;
}

function loadHtmlImage(blob) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const image = new Image();
    image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
    image.onerror = () => { URL.revokeObjectURL(url); reject(new Error('无法读取图片')); };
    image.src = url;
  });
}

async function maskToPng(blob, logits) {
  const original = await loadHtmlImage(blob);
  const maskHeight = logits.dims.at(-2);
  const maskWidth = logits.dims.at(-1);
  const maskCanvas = document.createElement('canvas');
  maskCanvas.width = maskWidth;
  maskCanvas.height = maskHeight;
  const maskContext = maskCanvas.getContext('2d');
  const maskImage = maskContext.createImageData(maskWidth, maskHeight);
  for (let index = 0; index < maskWidth * maskHeight; index += 1) {
    const probability = 1 / (1 + Math.exp(-logits.data[index]));
    const alpha = Math.max(0, Math.min(255, Math.round((probability - 0.28) / 0.42 * 255)));
    const offset = index * 4;
    maskImage.data[offset] = 255;
    maskImage.data[offset + 1] = 255;
    maskImage.data[offset + 2] = 255;
    maskImage.data[offset + 3] = alpha;
  }
  maskContext.putImageData(maskImage, 0, 0);

  const resultCanvas = document.createElement('canvas');
  resultCanvas.width = original.naturalWidth;
  resultCanvas.height = original.naturalHeight;
  const resultContext = resultCanvas.getContext('2d');
  resultContext.drawImage(original, 0, 0);
  resultContext.globalCompositeOperation = 'destination-in';
  resultContext.imageSmoothingEnabled = true;
  resultContext.drawImage(maskCanvas, 0, 0, resultCanvas.width, resultCanvas.height);
  return new Promise((resolve, reject) => resultCanvas.toBlob(
    value => value ? resolve(value) : reject(new Error('抠图结果生成失败')),
    'image/png',
  ));
}

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

async function runOpenAIAnalysis() {
  const form = new FormData();
  form.append('image', sourceBlob, sourceBlob.type === 'image/png' ? 'image.png' : 'image.jpg');
  form.append('prompt', cutoutPrompt.value.trim());
  const response = await fetch('/api/ai/subjects', { method: 'POST', body: form });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '云端识图失败');
  selectedAISubject = null;
  aiSubjectOptions.replaceChildren();
  (data.subjects || []).forEach(subject => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'subject-choice';
    button.textContent = subject.label;
    button.addEventListener('click', () => {
      selectedAISubject = subject;
      aiSubjectOptions.querySelectorAll('button').forEach(item => item.classList.toggle('selected', item === button));
      applyAiCutoutButton.disabled = false;
    });
    aiSubjectOptions.append(button);
  });
  aiSubjectPanel.classList.remove('hidden');
  cutoutMessage.textContent = (data.model || '识图模型') + ' 已给出主体选项，请选择后再抠图。';
}

async function runOpenAICutout() {
  if (!selectedAISubject) throw new Error('请先选择要抠出的主体。');
  const form = new FormData();
  form.append('image', sourceBlob, sourceBlob.type === 'image/png' ? 'image.png' : 'image.jpg');
  form.append('prompt', selectedAISubject.prompt);
  const response = await fetch('/api/ai/cutout', { method: 'POST', body: form });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '图像模型抠图失败');
  showCutoutResult(await dataUrlToBlob(data.image), (data.model || '图像模型') + ' 抠图完成，请确认结果。');
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

async function runLocalCutout() {
  const { RawImage, tokenizer, processor, model } = await getClipseg();
  const inputUrl = URL.createObjectURL(sourceBlob);
  let rawImage;
  try { rawImage = await RawImage.read(inputUrl); } finally { URL.revokeObjectURL(inputUrl); }
  const textInputs = tokenizer([promptForModel(cutoutPrompt.value)], { padding: true, truncation: true });
  const imageInputs = await processor(rawImage);
  const { logits } = await model({ ...textInputs, ...imageInputs });
  showCutoutResult(await maskToPng(sourceBlob, logits), '本地识图完成，请确认结果。');
}

cutoutButton.addEventListener('click', async () => {
  if (!sourceBlob) { cutoutMessage.textContent = '请先选择一张图片。'; return; }
  if (!cutoutPrompt.value.trim() && recognitionMode.value === 'local') {
    workingBlob = sourceBlob;
    proposedCutout = null;
    aiSubjectPanel.classList.add('hidden');
    cutoutResult.classList.add('hidden');
    cutoutMessage.textContent = '浏览器文字识图需要主体描述；未填写时已直接采用原图。';
    message.textContent = '已采用原图，可以生成图纸。';
    return;
  }
  cutoutButton.disabled = true;
  cutoutMessage.textContent = recognitionMode.value === 'openai'
    ? '正在分析图片中的主体选项…'
    : recognitionMode.value === 'image2'
      ? 'Image2 正在生成色块清晰的参考图，可能产生费用…'
      : recognitionMode.value === 'container'
        ? '容器正在运行本地分割模型，首次处理可能需要十几秒…'
        : '正在准备浏览器识别模型，首次使用可能需要几分钟…';
  try {
    if (recognitionMode.value === 'openai') await runOpenAIAnalysis();
    else if (recognitionMode.value === 'image2') await runImage2PatternReference();
    else if (recognitionMode.value === 'container') await runContainerCutout();
    else await runLocalCutout();
  } catch (error) {
    if (recognitionMode.value === 'local') clipsegPromise = null;
    cutoutMessage.textContent = '识别失败：' + (error.message || '请检查网络后重试');
  } finally {
    cutoutButton.disabled = false;
  }
});

applyAiCutoutButton.addEventListener('click', async () => {
  applyAiCutoutButton.disabled = true;
  cutoutMessage.textContent = '正在使用图像编辑模型抠图…';
  try {
    await runOpenAICutout();
  } catch (error) {
    cutoutMessage.textContent = '抠图失败：' + (error.message || '请检查网络后重试');
  } finally {
    applyAiCutoutButton.disabled = !selectedAISubject;
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

function refreshMaterials() {
  const { counts, total, rows, columns, empty } = summarizeGrid(patternGrid);
  $('#total').textContent = `${columns} × ${rows} · ${total} 颗${empty ? ` · ${empty} 格留空` : ''}`;
  const items = [...counts.entries()]
    .map(([code, count]) => ({ ...colourByCode.get(code), count }))
    .sort((a, b) => b.count - a.count);
  $('#palette').innerHTML = items.map(item => `
    <div class="swatch-row">
      <span class="swatch" style="background:rgb(${item.rgb.join(',')})"></span>
      <span><strong>${item.code}</strong><small>MARD 2.6 mm</small></span>
      <strong>${item.count} 颗</strong>
    </div>`).join('');
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

function showDirectImage2Pattern(data) {
  patternGrid = [];
  selectedCell = null;
  editHistory = [];
  $('#pattern').src = data.image;
  $('#download').href = data.image;
  $('#pattern').classList.remove('hidden');
  canvas.classList.add('hidden');
  $('#pixel-editor').classList.add('hidden');
  $('#total').textContent = 'Image2 直接生成';
  $('#palette').innerHTML = '<p class="status">此图纸由 Image2 直接生成，当前不提供可验证的 MARD 用量统计或逐格编辑。</p>';
}

generateButton.addEventListener('click', async () => {
  if (!workingBlob) { message.textContent = '请先选择并确认抠图结果。'; return; }
  const form = new FormData();
  form.append('image', workingBlob, workingBlob.type === 'image/png' ? 'subject.png' : 'subject.jpg');
  form.append('board', $('#board').value);
  generateButton.disabled = true;
  message.textContent = '正在交由 Image2 生成拼豆图纸，可能产生费用…';
  try {
    const response = await fetch('/api/ai/generate-pattern', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Image2 图纸生成失败');
    showDirectImage2Pattern(data);
    $('#result').classList.remove('hidden');
    message.textContent = (data.engine || 'Image2') + ' 图纸已生成，请下载并核对格线与色号。';
    $('#result').scrollIntoView({ behavior: 'smooth' });
  } catch (error) {
    message.textContent = error.message;
  } finally {
    generateButton.disabled = false;
  }
});

loadVersion();
loadConfig();
loadBoards();
