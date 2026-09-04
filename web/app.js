const imeInput = document.querySelector('#imeInput');
const connectionText = document.querySelector('#connectionText');
const statusDot = document.querySelector('#statusDot');
const activity = document.querySelector('#activity');
const clearButton = document.querySelector('#clearButton');
const newlineButton = document.querySelector('#newlineButton');
const adjustButton = document.querySelector('#adjustButton');
const sendButton = document.querySelector('#sendButton');
const imageButton = document.querySelector('#imageButton');
const fileButton = document.querySelector('#fileButton');
const imageInput = document.querySelector('#imageInput');
const fileInput = document.querySelector('#fileInput');
const controls = [newlineButton, sendButton, adjustButton, imageButton, fileButton];

const token = new URLSearchParams(location.search).get('token');
const clientId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
let socket;
let connected = false;
let sequence = 0;
let debounceTimer;
let reconnectTimer;
let composing = false;
let syncedValue = '';
let pending = null;
const commandQueue = [];

function setConnection(state) {
  connected = state === 'connected';
  imeInput.disabled = !connected;
  clearButton.disabled = !connected;
  controls.forEach(control => { control.disabled = !connected; });
  statusDot.className = `status-dot ${connected ? '' : state}`;
  if (connected) {
    connectionText.textContent = '电脑已连接';
    imeInput.placeholder = '点这里开始输入';
  } else if (state === 'offline') {
    connectionText.textContent = '连接已断开，正在重试';
    imeInput.placeholder = '等待重新连接电脑';
  } else {
    connectionText.textContent = '正在连接电脑';
    imeInput.placeholder = '正在连接电脑';
  }
  updateClearButton();
}

function updateClearButton() {
  clearButton.classList.toggle('visible', connected && imeInput.value.length > 0);
}

function connect() {
  clearTimeout(reconnectTimer);
  if (!token) {
    connectionText.textContent = '请扫描电脑上的二维码';
    setConnection('offline');
    return;
  }
  setConnection('connecting');
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${protocol}//${location.host}/ws?token=${encodeURIComponent(token)}`);
  socket.addEventListener('open', () => {
    setConnection('connected');
    if (pending) socket.send(JSON.stringify(pending.payload));
    else pump();
  });
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.type === 'ack' && pending && message.sequence === pending.payload.sequence) {
      if (pending.targetValue !== undefined) syncedValue = pending.targetValue;
      const completed = pending;
      const completedType = pending.payload.type;
      pending = null;
      if (completedType === 'send' || completedType === 'adjust') {
        const submittedValue = completed.clearValue ?? '';
        if (imeInput.value.startsWith(submittedValue)) {
          imeInput.value = imeInput.value.slice(submittedValue.length);
        }
        syncedValue = '';
        updateClearButton();
        activity.textContent = completedType === 'send'
          ? '已发送，输入框已清空'
          : '已调整方向，输入框已清空';
      } else {
        activity.textContent = '已同步到电脑';
      }
      pump();
    } else if (message.type === 'error') {
      pending = null;
      activity.textContent = `发送失败：${message.message}`;
    }
  });
  socket.addEventListener('close', () => {
    setConnection('offline');
    reconnectTimer = setTimeout(connect, 1600);
  });
  socket.addEventListener('error', () => socket.close());
}

function commonPrefixLength(oldValue, newValue) {
  const oldChars = Array.from(oldValue);
  const newChars = Array.from(newValue);
  let index = 0;
  while (index < oldChars.length && index < newChars.length && oldChars[index] === newChars[index]) index += 1;
  return { index, oldChars, newChars };
}

function sendPayload(type, extra = {}, targetValue) {
  if (!connected || socket.readyState !== WebSocket.OPEN || pending) return false;
  sequence += 1;
  const payload = { type, sequence, clientId, ...extra };
  const clearValue = type === 'send' || type === 'adjust' ? imeInput.value : undefined;
  pending = { payload, targetValue, clearValue };
  socket.send(JSON.stringify(payload));
  return true;
}

function flushText() {
  clearTimeout(debounceTimer);
  if (composing || pending) return false;
  const currentValue = imeInput.value;
  if (currentValue === syncedValue) return true;
  const diff = commonPrefixLength(syncedValue, currentValue);
  const remove = diff.oldChars.length - diff.index;
  const value = diff.newChars.slice(diff.index).join('');
  if (sendPayload('sync', { remove, value }, currentValue)) {
    activity.textContent = remove ? `正在同步删除 ${remove} 个字符…` : '正在同步输入…';
    return true;
  }
  return false;
}

function pump() {
  if (!connected || pending || composing) return;
  if (imeInput.value !== syncedValue) {
    flushText();
    return;
  }
  const command = commandQueue.shift();
  if (command) sendPayload(command);
}

function scheduleFlush() {
  clearTimeout(debounceTimer);
  if (!composing) debounceTimer = setTimeout(pump, 150);
}

function insertAtSelection(value) {
  const start = imeInput.selectionStart;
  const end = imeInput.selectionEnd;
  imeInput.setRangeText(value, start, end, 'end');
  imeInput.dispatchEvent(new InputEvent('input', { inputType: 'insertLineBreak', data: value }));
}

function queueCommand(type) {
  commandQueue.push(type);
  pump();
}

async function uploadSelected(file, kind) {
  if (!file || !connected) return;
  if (file.size > 100 * 1024 * 1024) {
    activity.textContent = '文件不能超过 100 MB';
    return;
  }
  const form = new FormData();
  form.append('upload', file, file.name);
  activity.textContent = `正在上传 ${file.name}…`;
  controls.forEach(control => { control.disabled = true; });
  try {
    const response = await fetch(`/upload?token=${encodeURIComponent(token)}&kind=${kind}`, {
      method: 'POST',
      body: form,
    });
    if (!response.ok) throw new Error(await response.text());
    activity.textContent = `已粘贴：${file.name}`;
  } catch (error) {
    activity.textContent = `上传失败：${error.message}`;
  } finally {
    controls.forEach(control => { control.disabled = !connected; });
    imageInput.value = '';
    fileInput.value = '';
  }
}

imeInput.addEventListener('focus', () => {
  activity.textContent = '现在可以打字或使用输入法语音';
});

imeInput.addEventListener('blur', pump);

imeInput.addEventListener('compositionstart', () => {
  composing = true;
  clearTimeout(debounceTimer);
  activity.textContent = '正在识别…';
});

imeInput.addEventListener('compositionend', () => {
  composing = false;
  pump();
});

imeInput.addEventListener('input', () => {
  activity.textContent = '正在接收手机输入…';
  updateClearButton();
  scheduleFlush();
});

clearButton.addEventListener('pointerdown', event => event.preventDefault());
clearButton.addEventListener('click', () => {
  if (!imeInput.value) return;
  imeInput.value = '';
  updateClearButton();
  activity.textContent = '正在清空电脑输入…';
  pump();
  imeInput.focus({ preventScroll: true });
});

newlineButton.addEventListener('click', () => {
  imeInput.focus({ preventScroll: true });
  insertAtSelection('\n');
  pump();
});

sendButton.addEventListener('click', () => queueCommand('send'));
adjustButton.addEventListener('click', () => queueCommand('adjust'));
imageButton.addEventListener('click', () => imageInput.click());
fileButton.addEventListener('click', () => fileInput.click());
imageInput.addEventListener('change', () => uploadSelected(imageInput.files[0], 'image'));
fileInput.addEventListener('change', () => uploadSelected(fileInput.files[0], 'file'));

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && (!socket || socket.readyState > WebSocket.OPEN)) connect();
});

setConnection('connecting');
connect();
