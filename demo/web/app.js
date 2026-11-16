const historyPanel = document.querySelector('#history');
const form = document.querySelector('#chat-form');
const input = document.querySelector('#message');
const submit = document.querySelector('#send');
const reset = document.querySelector('#reset');
const shortcuts = [...document.querySelectorAll('[data-prompt]')];
const leadList = document.querySelector('#lead-list');
const refresh = document.querySelector('#refresh');
const chatStatus = document.querySelector('#chat-status');
const managerDialog = document.querySelector('#manager-dialog');
const managerTitle = document.querySelector('#manager-title');
const managerHistory = document.querySelector('#manager-history');
const managerInput = document.querySelector('#manager-message');
const managerSend = document.querySelector('#manager-send');
const managerStatus = document.querySelector('#manager-status');
const newId = () => crypto.randomUUID();
let session = sessionStorage.getItem('leaf-session') || newId();
sessionStorage.setItem('leaf-session', session);
let ownedLead = null;
let selectedLead = null;
let clientRetry = null;
let managerRetry = null;
let managerBusy = false;
let busy = false;

function entry(who, text, sources = [], mode = '', usesContext = false) {
  const row = document.createElement('div');
  row.className = `turn ${who}`;
  const message = document.createElement('p');
  message.textContent = text;
  row.append(message);
  for (const source of sources) {
    const reference = document.createElement('small');
    reference.textContent = `Из справочника · ${source}`;
    row.append(reference);
  }
  if (mode) {
    const evidence = document.createElement('small');
    evidence.className = 'route';
    evidence.textContent = mode + (usesContext ? ' · учтен предыдущий вопрос' : '');
    row.append(evidence);
  }
  historyPanel.append(row);
  historyPanel.scrollTop = historyPanel.scrollHeight;
  return row;
}

function welcome() {
  historyPanel.replaceChildren();
  historyPanel.dataset.snapshot = '';
  entry('agent', 'Здравствуйте! Отвечу про аренду растений и помогу оставить заявку.');
}

async function request(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Не удалось выполнить запрос');
  return data;
}

function post(url, body) {
  return request(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
}

function renderTranscript(panel, messages) {
  const snapshot = JSON.stringify(messages);
  if (panel.dataset.snapshot === snapshot) return;
  panel.dataset.snapshot = snapshot;
  const labels = {user: 'Клиент', agent: 'Ассистент', manager: 'Менеджер'};
  panel.replaceChildren(...messages.map(item => {
    const row = document.createElement('div');
    row.className = `turn ${item.role}`;
    const label = document.createElement('small');
    label.textContent = labels[item.role];
    const message = document.createElement('p');
    message.textContent = item.text;
    row.append(label, message);
    return row;
  }));
  panel.scrollTop = panel.scrollHeight;
}

async function refreshClientDialog() {
  const current = session;
  const dialog = await request('/api/dialog?' + new URLSearchParams({session: current}));
  if (current !== session || !dialog.lead_id) return;
  ownedLead = dialog.lead_id;
  chatStatus.textContent = `Заявка №${ownedLead} · передано менеджеру`;
  chatStatus.hidden = false;
  shortcuts.forEach(button => { button.hidden = true; });
  renderTranscript(historyPanel, dialog.messages);
}

async function refreshManagerDialog(id) {
  const dialog = await request('/api/dialog?' + new URLSearchParams({lead_id: id}));
  if (id !== selectedLead || !dialog.lead_id) return;
  renderTranscript(managerHistory, dialog.messages);
}

async function openDialog(id) {
  if (id === selectedLead) return;
  selectedLead = id;
  managerDialog.hidden = false;
  managerTitle.textContent = `Заявка №${id} · переписка`;
  managerHistory.textContent = 'Загружаю переписку…';
  managerHistory.dataset.snapshot = '';
  managerInput.value = '';
  managerStatus.hidden = true;
  managerRetry = null;
  try {
    await refreshManagerDialog(id);
  } catch {
    if (id === selectedLead) managerHistory.textContent = 'Переписка пока недоступна. Обновите список.';
  }
}

async function updateLeads() {
  refresh.disabled = true;
  try {
    const {leads} = await request('/api/leads');
    leadList.replaceChildren();
    if (!leads.length) {
      leadList.textContent = 'Заявок пока нет';
      return;
    }
    const numberFormat = new Intl.DateTimeFormat('ru-RU', {hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow'});
    for (const lead of leads) {
      const row = document.createElement('article');
      row.className = 'lead';
      const label = document.createElement('small');
      label.textContent = `№${lead.id} · ${numberFormat.format(new Date(lead.created_at))} МСК`;
      const name = document.createElement('strong');
      name.textContent = lead.name;
      const phone = document.createElement('span');
      phone.textContent = lead.phone.replace(/^(\+7)(\d{3})(\d{3})(\d{2})(\d{2})$/, '$1 $2 $3-$4-$5');
      row.append(label, name, phone);
      if (lead.request) {
        const request = document.createElement('p');
        request.className = 'lead-request';
        request.textContent = lead.request;
        row.append(request);
      }
      if (lead.has_dialog) {
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'plain';
        open.textContent = 'Открыть переписку';
        open.addEventListener('click', () => openDialog(lead.id));
        row.append(open);
      }
      leadList.append(row);
    }
  } catch {
    leadList.textContent = 'Тестовая СРМ недоступна. Попробуйте обновить список.';
  } finally {
    refresh.disabled = false;
  }
}

async function send(text) {
  if (busy || !text.trim()) return;
  busy = true;
  [submit, reset, ...shortcuts].forEach(button => { button.disabled = true; });
  input.value = '';
  const row = entry('user', text);
  const pending = ownedLead ? null : entry('agent', 'Проверяю…');
  const event = clientRetry?.text === text ? clientRetry.event : newId();
  clientRetry = {text, event};
  try {
    const result = await post('/api/chat', {session, message: text, event});
    pending?.remove();
    clientRetry = null;
    if (result.kind === 'lead' || result.kind === 'manager') {
      ownedLead = result.lead_id;
      historyPanel.dataset.snapshot = '';
      await refreshClientDialog();
      if (result.kind === 'lead') await updateLeads();
      if (selectedLead === ownedLead) await refreshManagerDialog(ownedLead);
    } else {
      entry('agent', result.text, result.sources, result.mode, result.trace?.some(row => row.context));
    }
  } catch {
    row.remove();
    pending?.remove();
    if (clientRetry) {
      input.value ||= text;
      entry('agent', 'Запись пока не подтверждена. Повторите отправку того же сообщения.');
    }
  } finally {
    busy = false;
    [submit, reset, ...shortcuts].forEach(button => { button.disabled = false; });
    input.focus();
  }
}

document.querySelector('#manager-form').addEventListener('submit', async event => {
  event.preventDefault();
  const lead = selectedLead;
  const message = managerInput.value.trim();
  if (!lead || !message || managerBusy) return;
  managerBusy = true;
  managerSend.disabled = true;
  managerStatus.hidden = true;
  const key = managerRetry?.lead === lead && managerRetry?.message === message ? managerRetry.key : newId();
  managerRetry = {lead, message, key};
  try {
    await post('/api/reply', {lead_id: lead, message, event: key});
    managerRetry = null;
    if (lead === selectedLead) {
      managerInput.value = '';
      managerStatus.textContent = 'Ответ в переписке клиента';
      managerStatus.hidden = false;
      await refreshManagerDialog(lead);
    }
    if (lead === ownedLead) await refreshClientDialog();
  } catch {
    if (lead === selectedLead && managerRetry) {
      managerStatus.textContent = 'Отправка пока не подтверждена. Повторите ответ с тем же текстом.';
      managerStatus.hidden = false;
    }
  } finally {
    managerBusy = false;
    managerSend.disabled = false;
  }
});

form.addEventListener('submit', event => { event.preventDefault(); send(input.value); });
refresh.addEventListener('click', () => {
  updateLeads();
  if (selectedLead) refreshManagerDialog(selectedLead).catch(() => {});
});
reset.addEventListener('click', () => {
  session = newId();
  sessionStorage.setItem('leaf-session', session);
  ownedLead = null;
  clientRetry = null;
  chatStatus.hidden = true;
  shortcuts.forEach(button => { button.hidden = false; });
  input.value = '';
  welcome();
  input.focus();
});
shortcuts.forEach(button => button.addEventListener('click', () => send(button.dataset.prompt)));
welcome();
updateLeads();
refreshClientDialog().catch(() => {});
setInterval(() => {
  if (ownedLead && !busy) refreshClientDialog().catch(() => {});
  if (selectedLead && !managerBusy) refreshManagerDialog(selectedLead).catch(() => {});
}, 2000);
