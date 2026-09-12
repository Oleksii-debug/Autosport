(() => {
  'use strict';

  const demoEvents = [
    { match: 'TT-001', market: 'winner', selection: 'Player A', odds: '1.62', time: '12:00:00.000' },
    { match: 'TT-001', market: 'winner', selection: 'Player B', odds: '2.25', time: '12:00:00.000' },
    { match: 'TT-001', market: 'winner', selection: 'Player A', odds: '1.84', time: '12:00:03.250' },
    { match: 'TT-001', market: 'winner', selection: 'Player B', odds: '1.96', time: '12:00:03.250' },
  ];

  const state = { events: [], index: 0, latestBySelection: new Map() };
  const loadButton = document.getElementById('load-demo');
  const nextButton = document.getElementById('next-event');
  const rows = document.getElementById('market-rows');
  const summary = document.getElementById('market-summary');
  const experimentStatus = document.getElementById('experiment-status');
  const announcement = document.getElementById('announcement');

  function announce(message) {
    announcement.textContent = '';
    window.setTimeout(() => { announcement.textContent = message; }, 10);
  }

  function renderMarket() {
    rows.replaceChildren();
    const values = Array.from(state.latestBySelection.values());
    if (values.length === 0) {
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 5;
      cell.textContent = 'Дані ще не завантажено.';
      row.appendChild(cell);
      rows.appendChild(row);
    } else {
      for (const item of values) {
        const row = document.createElement('tr');
        for (const value of [item.match, item.market, item.selection, item.odds, item.time]) {
          const cell = document.createElement('td');
          cell.textContent = value;
          row.appendChild(cell);
        }
        rows.appendChild(row);
      }
    }
    summary.textContent = `Активних selections: ${values.length}. Випущено replay-подій: ${state.index} з ${state.events.length}.`;
  }

  loadButton.addEventListener('click', () => {
    state.events = demoEvents.slice();
    state.index = 0;
    state.latestBySelection.clear();
    nextButton.disabled = false;
    experimentStatus.textContent = 'Режим: локальний демонстраційний causal replay. Події завантажено; майбутні значення ще не показані.';
    renderMarket();
    announce('Демонстраційні події завантажено.');
  });

  nextButton.addEventListener('click', () => {
    if (state.index >= state.events.length) return;
    const event = state.events[state.index++];
    state.latestBySelection.set(`${event.match}|${event.market}|${event.selection}`, event);
    renderMarket();
    announce(`Оновлено коефіцієнт для ${event.selection}.`);
    if (state.index >= state.events.length) {
      nextButton.disabled = true;
      experimentStatus.textContent = 'Демонстраційний replay завершено. Усі випущені дані залишаються видимими та копійованими на сторінці.';
    }
  });
})();
