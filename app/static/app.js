const fileInput = document.querySelector('#file');
const dropZone = document.querySelector('#drop-zone');
const fileLabel = document.querySelector('#file-label');
const button = document.querySelector('#generate');
const message = document.querySelector('#message');

fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) fileLabel.textContent = fileInput.files[0].name;
});
['dragenter', 'dragover'].forEach(name => dropZone.addEventListener(name, event => {
  event.preventDefault(); dropZone.classList.add('drag');
}));
['dragleave', 'drop'].forEach(name => dropZone.addEventListener(name, event => {
  event.preventDefault(); dropZone.classList.remove('drag');
}));
dropZone.addEventListener('drop', event => {
  if (event.dataTransfer.files.length) {
    fileInput.files = event.dataTransfer.files;
    fileLabel.textContent = event.dataTransfer.files[0].name;
  }
});

button.addEventListener('click', async () => {
  if (!fileInput.files[0]) { message.textContent = '请先选择一张图片。'; return; }
  const form = new FormData();
  form.append('image', fileInput.files[0]);
  form.append('width', document.querySelector('#width').value);
  form.append('height', document.querySelector('#height').value);
  form.append('colours', document.querySelector('#colours').value);
  button.disabled = true; message.textContent = '正在生成…';
  try {
    const response = await fetch('/api/generate', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '生成失败');
    document.querySelector('#pattern').src = data.image;
    document.querySelector('#download').href = data.image;
    document.querySelector('#total').textContent = `${data.width} × ${data.height} · 共 ${data.total} 颗`;
    document.querySelector('#palette').innerHTML = data.palette.map(item => `
      <div class="swatch-row">
        <span class="swatch" style="background:rgb(${item.rgb.join(',')})"></span>
        <span><strong>${item.code}</strong> ${item.name}</span>
        <strong>${item.count} 颗</strong>
      </div>`).join('');
    document.querySelector('#result').classList.remove('hidden');
    message.textContent = '生成完成。';
    document.querySelector('#result').scrollIntoView({ behavior: 'smooth' });
  } catch (error) {
    message.textContent = error.message;
  } finally { button.disabled = false; }
});
