// Инициализация Telegram Web App
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const API_URL = '';

let userData = null;
let eventsData = null;
let currentCalendarYear = new Date().getFullYear();
let currentCalendarMonth = new Date().getMonth();

// Даты с записанными моментами (грызли) — для календаря красный
function getDatesWithEvents() {
    const set = new Set();
    const events = eventsData?.events || [];
    events.forEach(e => {
        if (e.datetime) {
            const d = e.datetime.slice(0, 10);
            set.add(d);
        }
    });
    return set;
}

const MONTH_NAMES = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
const LEVELS = [
    { days: 7, name: 'Базовый уровень' },
    { days: 14, name: 'Стойкий' },
    { days: 30, name: 'Контроль' },
    { days: 90, name: 'Свобода' },
];
const WEEKDAY_NAMES = ['Воскресенье', 'Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота'];
const WEEKDAY_ACCUSATIVE = ['воскресенье', 'понедельник', 'вторник', 'среду', 'четверг', 'пятницу', 'субботу'];
const WEEKDAY_DATIVE = ['воскресеньям', 'понедельникам', 'вторникам', 'средам', 'четвергам', 'пятницам', 'субботам'];
const WEEKDAY_SHORT = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];

function formatDateKey(year, month, day) {
    const m = String(month + 1).padStart(2, '0');
    const d = String(day).padStart(2, '0');
    return `${year}-${m}-${d}`;
}

function isToday(year, month, day) {
    const today = new Date();
    return today.getFullYear() === year && today.getMonth() === month && today.getDate() === day;
}

async function init() {
    try {
        const initData = tg.initData;
        if (!initData) {
            tg.showAlert('Ошибка: initData не доступен. Откройте мини-приложение кнопкой в боте.');
            return;
        }
        await loadUserData();
        await loadEventsData();
        updateMainScreen();
        setupEventHandlers();
        renderCalendar(new Date().getFullYear(), new Date().getMonth());
        setActiveNav('mainScreen');
    } catch (error) {
        console.error('Ошибка инициализации:', error);
        tg.showAlert('Ошибка загрузки данных. Попробуйте позже.');
    }
}

async function loadUserData() {
    try {
        const initData = tg.initData;
        if (!initData) throw new Error('initData не доступен');
        const url = (API_URL || window.location.origin) + '/api/user';
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData }),
        });
        const errorText = await response.text();
        if (!response.ok) {
            let msg = errorText || `Код ${response.status}`;
            try {
                const j = JSON.parse(errorText);
                if (j && j.error) msg = j.error;
            } catch (_) {}
            tg.showAlert('Ошибка загрузки: ' + msg);
            throw new Error(msg);
        }
        userData = JSON.parse(errorText || '{}');
    } catch (error) {
        console.error('Ошибка загрузки данных пользователя:', error);
        const msg = (error && error.message) ? error.message : String(error);
        if (!msg.includes('Ошибка загрузки:')) tg.showAlert('Ошибка: ' + msg);
        throw error;
    }
}

async function loadEventsData() {
    try {
        const initData = tg.initData;
        const response = await fetch((API_URL || window.location.origin) + '/api/events', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData }),
        });
        if (!response.ok) throw new Error('Ошибка загрузки событий');
        eventsData = await response.json();
    } catch (error) {
        console.error('Ошибка загрузки событий:', error);
        eventsData = { events: [], chartData: [] };
    }
}

function getNextLevel(streak) {
    for (const level of LEVELS) {
        if (streak < level.days) return level;
    }
    return LEVELS[LEVELS.length - 1];
}

function renderProgressBar() {
    const streak = userData?.current_streak ?? 0;
    const level = getNextLevel(streak);
    const filled = Math.min(streak, level.days);
    const total = level.days;
    document.getElementById('levelName').textContent = level.name;
    document.getElementById('progressLabel').textContent = `${filled}/${total} дней`;
    const bar = document.getElementById('progressBar');
    const maxSegments = 14;
    const segTotal = total <= maxSegments ? total : maxSegments;
    const segFilled = total <= maxSegments ? filled : Math.round((filled / total) * segTotal);
    let html = '';
    for (let i = 0; i < segTotal; i++) {
        const cls = i < segFilled ? 'progress-segment filled' : 'progress-segment empty';
        html += `<span class="${cls}"></span>`;
    }
    bar.innerHTML = html;
}

function getForecast() {
    const streak = userData?.current_streak ?? 0;
    const maxStreak = userData?.max_streak ?? 0;
    const level = getNextLevel(streak);
    const daysLeft = level.days - streak;
    const atMaxLevel = streak >= 90;
    const forecasts = [];
    if (streak < maxStreak && maxStreak > 0) {
        const toRecord = maxStreak - streak;
        forecasts.push(`Если ты продержишься ещё ${toRecord} ${toRecord === 1 ? 'день' : toRecord < 5 ? 'дня' : 'дней'} — это будет твоя самая длинная серия!`);
    }
    if (daysLeft > 0) {
        forecasts.push(`Ещё ${daysLeft} ${daysLeft === 1 ? 'день' : daysLeft < 5 ? 'дня' : 'дней'} до уровня «${level.name}».`);
    }
    if (level.days > 0 && streak > 0 && !atMaxLevel) {
        const pct = Math.round((streak / level.days) * 100);
        forecasts.push(`Ты уже прошёл ${pct}% пути до следующего уровня.`);
    }
    const dayWord = (n) => (n % 10 === 1 && n % 100 !== 11) ? 'день' : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20)) ? 'дня' : 'дней';
    forecasts.push(`Твоя серия — это ${streak} ${dayWord(streak)} силы воли. Продолжай!`);
    forecasts.push(atMaxLevel ? 'Ты достиг уровня «Свобода»! Держи планку!' : 'Каждый день без грызения делает тебя ближе к свободе.');
    const idx = new Date().getDate() % Math.max(1, forecasts.length);
    return forecasts[idx];
}

function getDailyPhrase() {
    const events = eventsData?.events || [];
    const today = new Date().toISOString().slice(0, 10);
    const bitToday = events.some(e => e.datetime && e.datetime.startsWith(today));
    const streak = userData?.current_streak ?? 0;
    const phrasesAll = [
        'Ты уже сильнее, чем вчера.',
        'Один день — это победа.',
        'Срывы бывают. Важно не останавливаться.',
        'Сегодня не получилось — завтра получится.',
        'Один срыв не отменяет твоих побед.',
        'Ты держишься отлично. Так держать!',
        'Каждый день — шаг к свободе.',
        'Ты молодец, что возвращаешься снова.',
        'Не идеал важен, а движение вперёд.',
        'Маленькие победы ведут к большим.',
        'Завтра — новый день и новый шанс.',
        'Ты справляешься лучше, чем думаешь.',
    ];
    let pool = phrasesAll;
    if (bitToday && streak === 0) pool = phrasesAll.slice(2, 6);
    else if (bitToday) pool = phrasesAll.slice(2, 5);
    else if (streak >= 7) pool = phrasesAll.slice(5, 8).concat(phrasesAll.slice(0, 2));
    const dayOfYear = Math.floor((new Date() - new Date(new Date().getFullYear(), 0, 0)) / 86400000);
    return pool[dayOfYear % pool.length];
}

function updateMainScreen() {
    if (!userData) return;
    document.getElementById('userName').textContent = userData.name || 'друг';
    document.getElementById('currentStreak').textContent = userData.current_streak ?? 0;
    document.getElementById('dailyPhrase').textContent = getDailyPhrase();
    renderProgressBar();
    document.getElementById('forecastText').textContent = getForecast();
}

function computeAnalytics() {
    const events = eventsData?.events || [];
    const now = new Date();
    // Неделя начинается с понедельника: (getDay() + 6) % 7 — дней с понедельника (Вс=6, Пн=0)
    const daysSinceMonday = (now.getDay() + 6) % 7;
    const weekStart = new Date(now);
    weekStart.setDate(now.getDate() - daysSinceMonday);
    weekStart.setHours(0, 0, 0, 0);
    const dayMs = 24 * 3600 * 1000;
    const daysThisWeek = Math.ceil((now - weekStart) / dayMs) || 1;

    if (events.length === 0) {
        return {
            topDay: '—',
            topHour: '—',
            weekTopDay: '—',
            avgPerMonth: '—',
            percentClean: '—',
        };
    }

    const datesSet = new Set();
    let firstDate = null;
    const weekByDay = [0, 0, 0, 0, 0, 0, 0];
    const weekByHourSlot = new Array(12).fill(0);

    // datetime приходит в UTC (с суффиксом Z). getHours()/getDay() дают локальное время пользователя.
    events.forEach(e => {
        if (!e.datetime) return;
        const dt = new Date(e.datetime);
        if (isNaN(dt.getTime())) return;
        const d = e.datetime.slice(0, 10);
        datesSet.add(d);
        if (!firstDate || d < firstDate) firstDate = d;
        if (dt >= weekStart) {
            weekByDay[dt.getDay()]++;
            const slot = Math.floor(dt.getHours() / 2);
            weekByHourSlot[slot]++;
        }
    });

    const topDayIdx = weekByDay.indexOf(Math.max(...weekByDay));
    const topDay = Math.max(...weekByDay) > 0 ? WEEKDAY_ACCUSATIVE[topDayIdx] : '—';

    const topSlotIdx = weekByHourSlot.indexOf(Math.max(...weekByHourSlot));
    const startHour = topSlotIdx * 2;
    const endHour = Math.min(startHour + 2, 24);
    const topHour = Math.max(...weekByHourSlot) > 0
        ? `${String(startHour).padStart(2, '0')}:00-${String(endHour).padStart(2, '0')}:00`
        : '—';

    const weekTopIdx = weekByDay.indexOf(Math.max(...weekByDay));
    const weekTopDay = Math.max(...weekByDay) > 0 ? WEEKDAY_DATIVE[weekTopIdx] : '—';

    const eventsThisWeek = events.filter(e => {
        if (!e.datetime) return false;
        return new Date(e.datetime) >= weekStart;
    }).length;
    const avgPerMonth = Math.ceil(eventsThisWeek / daysThisWeek).toString();

    const first = new Date(firstDate);
    const totalDays = Math.max(1, Math.ceil((now - first) / dayMs));
    const daysWithEvents = datesSet.size;
    const percentClean = Math.round(((totalDays - daysWithEvents) / totalDays) * 100);

    return {
        topDay,
        topHour,
        weekTopDay,
        avgPerMonth,
        percentClean: percentClean + '%',
    };
}

function updateStatsScreen() {
    if (!userData || !eventsData) return;
    document.getElementById('daysWithout').textContent = userData.current_streak ?? 0;
    document.getElementById('eventsCount').textContent = eventsData.events?.length ?? 0;
    const a = computeAnalytics();
    document.getElementById('analyticsDay').textContent = a.topDay;
    document.getElementById('analyticsHour').textContent = a.topHour;
    document.getElementById('analyticsWeekDay').textContent = a.weekTopDay;
    document.getElementById('analyticsAvgMonth').textContent = a.avgPerMonth;
    document.getElementById('analyticsPercentClean').textContent = a.percentClean;
}

function setActiveNav(screenId) {
    document.querySelectorAll('.nav-btn').forEach(btn => btn.classList.remove('active'));
    const id = screenId === 'mainScreen' ? 'homeBtn' : screenId === 'statsScreen' ? 'statsBtn' : 'calendarBtn';
    const btn = document.getElementById(id);
    if (btn) btn.classList.add('active');
}

function setupEventHandlers() {
    document.getElementById('homeBtn').addEventListener('click', showMainScreen);
    document.getElementById('statsBtn').addEventListener('click', showStatsScreen);
    document.getElementById('calendarBtn').addEventListener('click', showCalendarScreen);

    document.getElementById('prevMonth').addEventListener('click', () => {
        currentCalendarMonth--;
        if (currentCalendarMonth < 0) { currentCalendarMonth = 11; currentCalendarYear--; }
        renderCalendar(currentCalendarYear, currentCalendarMonth);
        updateCalendarTitle(currentCalendarYear, currentCalendarMonth);
    });
    document.getElementById('nextMonth').addEventListener('click', () => {
        currentCalendarMonth++;
        if (currentCalendarMonth > 11) { currentCalendarMonth = 0; currentCalendarYear++; }
        renderCalendar(currentCalendarYear, currentCalendarMonth);
        updateCalendarTitle(currentCalendarYear, currentCalendarMonth);
    });
}

function showMainScreen() {
    document.getElementById('mainScreen').classList.add('active');
    document.getElementById('statsScreen').classList.remove('active');
    document.getElementById('calendarScreen').classList.remove('active');
    setActiveNav('mainScreen');
}

function showStatsScreen() {
    updateStatsScreen();
    document.getElementById('mainScreen').classList.remove('active');
    document.getElementById('statsScreen').classList.add('active');
    document.getElementById('calendarScreen').classList.remove('active');
    setActiveNav('statsScreen');
}

function showCalendarScreen() {
    renderCalendar(currentCalendarYear, currentCalendarMonth);
    updateCalendarTitle(currentCalendarYear, currentCalendarMonth);
    document.getElementById('mainScreen').classList.remove('active');
    document.getElementById('statsScreen').classList.remove('active');
    document.getElementById('calendarScreen').classList.add('active');
    setActiveNav('calendarScreen');
}

function updateCalendarTitle(year, month) {
    document.getElementById('monthTitle').textContent = MONTH_NAMES[month] + ' ' + year;
}

function renderCalendar(year, month) {
    const datesWithEvents = getDatesWithEvents();
    const firstDay = new Date(year, month, 1);
    const lastDay = new Date(year, month + 1, 0);
    const startWeekday = (firstDay.getDay() + 6) % 7;
    const daysInMonth = lastDay.getDate();
    const today = new Date();
    today.setHours(0, 0, 0, 0);

    let html = '';
    for (let i = 0; i < startWeekday; i++) {
        const prevMonthDay = new Date(year, month, 1 - (startWeekday - i));
        const dayNum = prevMonthDay.getDate();
        html += `<div class="calendar-day other-month"><span class="day-num">${dayNum}</span></div>`;
    }
    for (let day = 1; day <= daysInMonth; day++) {
        const key = formatDateKey(year, month, day);
        const cellDate = new Date(year, month, day);
        cellDate.setHours(0, 0, 0, 0);
        const isPast = cellDate < today;
        const hasEvents = datesWithEvents.has(key);
        let cls = 'calendar-day';
        if (isToday(year, month, day)) cls += ' today';
        if (isPast) cls += ' past';
        const dotClass = isPast ? (hasEvents ? 'red' : 'green') : '';
        const dotHtml = dotClass ? `<span class="day-dot ${dotClass}"></span>` : '';
        html += `<div class="${cls}"><span class="day-num">${day}</span>${dotHtml}</div>`;
    }
    document.getElementById('calendarGrid').innerHTML = html;
}

init();
