const historyPanel = document.querySelector('#history');
const form = document.querySelector('#chat-form');
const input = document.querySelector('#message');
const submit = document.querySelector('#send');
const reset = document.querySelector('#reset');
const shortcuts = [...document.querySelectorAll('[data-prompt]')];
const leadList = document.querySelector('#lead-list');
const refresh = document.querySelector('#refresh');
let session = crypto.randomUUID();
let busy = false;

function entry(who, text, source = '') {
  const row = document.createElement('div');
  row.className = `turn ${who}`;
  const message = document.createElement('p');
  message.textContent = text;
  row.append(message);
  if (source) {
    const reference = document.createElement('small');
    reference.textContent = `Из справочника · ${source}`;
    row.append(reference);
  }
  historyPanel.append(row);
  historyPanel.scrollTop = historyPanel.scrollHeight;
  return row;
}

function welcome() {
  historyPanel.replaceChildren();
  entry('agent', 'Здравствуйте! Отвечу про аренду растений и помогу оставить заявку.');
}

async function request(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Не удалось выполнить запрос');
  return data;
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
      phone.textContent = lead.phone;
      row.append(label, name, phone);
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
  const pending = entry('agent', 'Проверяю…');
  try {
    const result = await request('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session, message: text}),
    });
    pending.remove();
    entry('agent', result.text, result.source);
    if (result.kind === 'lead') await updateLeads();
  } catch {
    row.remove();
    pending.remove();
    input.value ||= text;
    entry('agent', 'Ответ не загрузился. Повторите отправку сообщения.');
  } finally {
    busy = false;
    [submit, reset, ...shortcuts].forEach(button => { button.disabled = false; });
    input.focus();
  }
}

form.addEventListener('submit', event => { event.preventDefault(); send(input.value); });
refresh.addEventListener('click', updateLeads);
reset.addEventListener('click', () => { session = crypto.randomUUID(); input.value = ''; welcome(); input.focus(); });
shortcuts.forEach(button => button.addEventListener('click', () => send(button.dataset.prompt)));
welcome();
updateLeads();
