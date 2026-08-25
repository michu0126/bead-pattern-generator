(() => {
  const passwordInput = document.querySelector('#settings-password');
  const message = document.querySelector('#api-log-message');
  const list = document.querySelector('#api-log-list');
  const refreshButton = document.querySelector('#refresh-api-logs');
  const clearButton = document.querySelector('#clear-api-logs');

  function text(value, fallback = '—') {
    return value === null || value === undefined || value === '' ? fallback : String(value);
  }

  async function requestLogs(method = 'GET') {
    const password = passwordInput.value;
    if (!password) throw new Error('请先输入设置管理密码。');
    const response = await fetch('/api/settings/logs?limit=100', {
      method,
      cache: 'no-store',
      headers: { 'X-Settings-Password': password },
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* 使用通用错误。 */ }
    if (!response.ok) throw new Error(data.detail || ('请求失败（' + response.status + '）'));
    return data;
  }

  function addDetail(container, label, value) {
    const row = document.createElement('div');
    const term = document.createElement('dt');
    const description = document.createElement('dd');
    term.textContent = label;
    description.textContent = text(value);
    row.append(term, description);
    container.append(row);
  }

  function renderLogs(logs) {
    list.replaceChildren();
    if (!logs.length) {
      const empty = document.createElement('p');
      empty.className = 'api-log-empty';
      empty.textContent = '尚无真实 Image API 调用记录。';
      list.append(empty);
      return;
    }

    logs.forEach(entry => {
      const card = document.createElement('article');
      card.className = 'api-log-entry ' + (entry.success ? 'success' : 'failure');

      const heading = document.createElement('div');
      heading.className = 'api-log-entry-heading';
      const title = document.createElement('strong');
      title.textContent = entry.success ? '调用成功' : (entry.dispatched ? '调用失败' : '未发出请求');
      const time = document.createElement('time');
      const parsed = new Date(entry.started_at);
      time.textContent = Number.isNaN(parsed.getTime()) ? text(entry.started_at) : parsed.toLocaleString('zh-CN');
      heading.append(title, time);

      const details = document.createElement('dl');
      addDetail(details, '模型', entry.model);
      addDetail(details, '接口', entry.endpoint);
      addDetail(details, '已发送', entry.dispatched ? '是' : '否');
      addDetail(details, 'HTTP 状态', entry.response?.status_code);
      addDetail(details, '耗时', entry.duration_ms === undefined ? null : entry.duration_ms + ' ms');
      addDetail(details, '请求 ID', entry.response?.request_id);
      addDetail(details, '输入大小', entry.input?.bytes === undefined ? null : entry.input.bytes + ' bytes');
      addDetail(details, '输出大小', entry.response?.output_bytes === undefined ? null : entry.response.output_bytes + ' bytes');
      addDetail(details, '透明像素', entry.response?.alpha?.transparent_percent === undefined ? null : entry.response.alpha.transparent_percent + '%');
      addDetail(details, 'Alpha 范围', entry.response?.alpha ? entry.response.alpha.alpha_min + '–' + entry.response.alpha.alpha_max : null);
      addDetail(details, '质量', entry.request?.quality);
      addDetail(details, '错误', entry.error);

      card.append(heading, details);
      list.append(card);
    });
  }

  refreshButton.addEventListener('click', async () => {
    refreshButton.disabled = true;
    message.textContent = '正在读取真实调用记录…';
    try {
      const data = await requestLogs();
      renderLogs(data.logs || []);
      message.textContent = '已读取 ' + (data.logs || []).length + ' 条记录。';
    } catch (error) {
      message.textContent = error.message;
    } finally {
      refreshButton.disabled = false;
    }
  });

  clearButton.addEventListener('click', async () => {
    if (!window.confirm('确定清空所有 Image API 调用日志吗？')) return;
    clearButton.disabled = true;
    try {
      const data = await requestLogs('DELETE');
      renderLogs([]);
      message.textContent = data.message;
    } catch (error) {
      message.textContent = error.message;
    } finally {
      clearButton.disabled = false;
    }
  });
})();
