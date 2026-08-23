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
const generateButton = $('#generate');
const message = $('#message');
const clearCacheButton = $('#clear-cache');

let sourceBlob = null;
let workingBlob = null;
let proposedCutout = null;
let sourceUrl = null;
let cutoutUrl = null;
let clipsegPromise = null;

const BOARD_FALLBACK = [
  { id: '50x50', label: '50 × 50（2.6 mm 标准单板）' },
  { id: '52x52', label: '52 × 52（2.6 mm 标准单板）' },
  { id: '100x50', label: '100 × 50（两块 50 板横拼）' },
  { id: '104x52', label: '104 × 52（两块 52 板横拼）' },
  { id: '100x100', label: '100 × 100（四块 50 板）' },
  { id: '104x104', label: '104 × 104（四块 52 板）' },
];

async function loadVersion() {
  try {
    const response = await fetch('/api/version', { cache: 'no-store' });
    if (!response.ok) throw new Error();
    const data = await response.json();
    $('#app-version').textContent = data.version;
    $('#release-title').textContent = `v${data.version} 更新内容`;
    $('#version-changes').innerHTML = data.changes.map(change => `<li>${change}</li>`).join('');
  } catch (_) {
    // HTML 内保留版本信息，旧后端或临时网络异常时仍然可见。
  }
}

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
    const separator = location.pathname.includes('?') ? '&' : '?';
    location.replace(`${location.pathname}${separator}refresh=${Date.now()}`);
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
  $('#board').innerHTML = boards.map(item => `<option value="${item.id}">${item.label}</option>`).join('');
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

  const canvas = document.createElement('canvas');
  canvas.width = original.naturalWidth;
  canvas.height = original.naturalHeight;
  const context = canvas.getContext('2d');
  context.drawImage(original, 0, 0);
  context.globalCompositeOperation = 'destination-in';
  context.imageSmoothingEnabled = true;
  context.drawImage(maskCanvas, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve, reject) => canvas.toBlob(
    value => value ? resolve(value) : reject(new Error('抠图结果生成失败')),
    'image/png',
  ));
}

cutoutButton.addEventListener('click', async () => {
  if (!sourceBlob) { cutoutMessage.textContent = '请先选择一张图片。'; return; }
  if (!cutoutPrompt.value.trim()) {
    workingBlob = sourceBlob;
    proposedCutout = null;
    cutoutResult.classList.add('hidden');
    cutoutMessage.textContent = '没有填写主体描述，已直接采用原图。';
    message.textContent = '已采用原图，可以生成图纸。';
    return;
  }
  cutoutButton.disabled = true;
  cutoutMessage.textContent = '正在准备识别模型并抠图，首次使用可能需要几分钟…';
  try {
    const { RawImage, tokenizer, processor, model } = await getClipseg();
    const inputUrl = URL.createObjectURL(sourceBlob);
    let rawImage;
    try { rawImage = await RawImage.read(inputUrl); } finally { URL.revokeObjectURL(inputUrl); }
    const textInputs = tokenizer([promptForModel(cutoutPrompt.value)], { padding: true, truncation: true });
    const imageInputs = await processor(rawImage);
    const { logits } = await model({ ...textInputs, ...imageInputs });
    proposedCutout = await maskToPng(sourceBlob, logits);
    clearObjectUrl(cutoutUrl);
    cutoutUrl = URL.createObjectURL(proposedCutout);
    cutoutPreview.src = cutoutUrl;
    cutoutResult.classList.remove('hidden');
    cutoutMessage.textContent = '抠图完成，请查看结果后选择采用或弃用。';
    cutoutResult.scrollIntoView({ behavior: 'smooth', block: 'center' });
  } catch (error) {
    clipsegPromise = null;
    cutoutMessage.textContent = `抠图失败：${error.message || '请检查网络后重试'}`;
  } finally {
    cutoutButton.disabled = false;
  }
});

$('#use-cutout').addEventListener('click', () => {
  if (!proposedCutout) return;
  workingBlob = proposedCutout;
  cutoutResult.classList.add('hidden');
  cutoutMessage.textContent = '已采用抠图结果，透明区域不会放豆。';
  message.textContent = '已采用抠图结果，可以生成图纸。';
});

$('#discard-cutout').addEventListener('click', () => {
  workingBlob = sourceBlob;
  proposedCutout = null;
  cutoutResult.classList.add('hidden');
  cutoutMessage.textContent = '已弃用抠图结果，继续使用原图。';
  message.textContent = '正在使用原图。';
});

generateButton.addEventListener('click', async () => {
  if (!workingBlob) { message.textContent = '请先选择一张图片。'; return; }
  const form = new FormData();
  form.append('image', workingBlob, workingBlob.type === 'image/png' ? 'image.png' : 'image.jpg');
  form.append('board', $('#board').value);
  generateButton.disabled = true;
  message.textContent = '正在匹配 MARD 色卡并绘制色号…';
  try {
    const response = await fetch('/api/generate', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '生成失败');
    $('#pattern').src = data.image;
    $('#download').href = data.image;
    const emptyText = data.empty ? ` · ${data.empty} 格留空` : '';
    $('#total').textContent = `${data.width} × ${data.height} · ${data.total} 颗${emptyText}`;
    $('#palette').innerHTML = data.palette.map(item => `
      <div class="swatch-row">
        <span class="swatch" style="background:rgb(${item.rgb.join(',')})"></span>
        <span><strong>${item.code}</strong><small>MARD 2.6 mm</small></span>
        <strong>${item.count} 颗</strong>
      </div>`).join('');
    $('#result').classList.remove('hidden');
    message.textContent = `生成完成，共使用 ${data.palette.length} 个 MARD 色号。`;
    $('#result').scrollIntoView({ behavior: 'smooth' });
  } catch (error) {
    message.textContent = error.message;
  } finally {
    generateButton.disabled = false;
  }
});

loadVersion();
loadBoards();
