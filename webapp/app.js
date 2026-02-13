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

function updateMainScreen() {
    if (!userData) return;
    document.getElementById('userName').textContent = userData.name || 'друг';
    document.getElementById('currentStreak').textContent = userData.current_streak ?? 0;
}

function updateStatsScreen() {
    if (!userData || !eventsData) return;
    document.getElementById('daysWithout').textContent = userData.current_streak ?? 0;
    document.getElementById('eventsCount').textContent = eventsData.events?.length ?? 0;
    drawChart(eventsData.chartData || []);
}

function setupEventHandlers() {
    document.getElementById('statsBtn').addEventListener('click', showStatsScreen);
    document.getElementById('calendarBtn').addEventListener('click', showCalendarScreen);
    document.getElementById('backBtnStats').addEventListener('click', showMainScreen);
    document.getElementById('backBtnCalendar').addEventListener('click', showMainScreen);

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
}

function showStatsScreen() {
    updateStatsScreen();
    document.getElementById('mainScreen').classList.remove('active');
    document.getElementById('statsScreen').classList.add('active');
    document.getElementById('calendarScreen').classList.remove('active');
}

function showCalendarScreen() {
    renderCalendar(currentCalendarYear, currentCalendarMonth);
    updateCalendarTitle(currentCalendarYear, currentCalendarMonth);
    document.getElementById('mainScreen').classList.remove('active');
    document.getElementById('statsScreen').classList.remove('active');
    document.getElementById('calendarScreen').classList.add('active');
}

function updateCalendarTitle(year, month) {
    document.getElementById('monthTitle').textContent = MONTH_NAMES[month] + ' ' + year;
}

function renderCalendar(year, month) {
    const datesWithEvents = getDatesWithEvents();
    const firstDay = new Date(year, month, 1);
    const lastDay = new Date(year, month + 1, 0);
    const startWeekday = (firstDay.getDay() + 6) % 7; // Пн = 0
    const daysInMonth = lastDay.getDate();
    const today = new Date();
    today.setHours(0, 0, 0, 0);

    let html = '';
    for (let i = 0; i < startWeekday; i++) {
        const prevMonthDay = new Date(year, month, 1 - (startWeekday - i));
        const dayNum = prevMonthDay.getDate();
        html += `<div class="calendar-day other-month">${dayNum}</div>`;
    }
    for (let day = 1; day <= daysInMonth; day++) {
        const key = formatDateKey(year, month, day);
        const cellDate = new Date(year, month, day);
        cellDate.setHours(0, 0, 0, 0);
        const isPast = cellDate < today;
        const hasEvents = datesWithEvents.has(key);
        let cls = 'calendar-day';
        if (isToday(year, month, day)) cls += ' today';
        if (isPast) cls += hasEvents ? ' red' : ' green';
        html += `<div class="${cls}">${day}</div>`;
    }
    document.getElementById('calendarGrid').innerHTML = html;
}

function drawChart(chartData) {
    const canvas = document.getElementById('streakChart');
    const ctx = canvas.getContext('2d');
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    const width = rect.width;
    const height = rect.height;
    ctx.clearRect(0, 0, width, height);

    if (!chartData || chartData.length === 0) {
        ctx.fillStyle = '#5a7a8c';
        ctx.font = '14px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('Данных пока нет', width / 2, height / 2);
        return;
    }

    const maxValue = Math.max(...chartData.map(d => d.value), 1);
    const padding = 20;
    const chartWidth = width - padding * 2;
    const chartHeight = height - padding * 2;

    ctx.strokeStyle = 'rgba(90, 122, 140, 0.3)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding, padding);
    ctx.lineTo(padding, height - padding);
    ctx.lineTo(width - padding, height - padding);
    ctx.stroke();

    ctx.strokeStyle = '#5a8fa8';
    ctx.lineWidth = 2;
    ctx.beginPath();
    const stepX = chartWidth / Math.max(chartData.length - 1, 1);
    chartData.forEach((point, index) => {
        const x = padding + index * stepX;
        const y = height - padding - (point.value / maxValue) * chartHeight;
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();

    ctx.fillStyle = '#5a8fa8';
    chartData.forEach((point, index) => {
        const x = padding + index * stepX;
        const y = height - padding - (point.value / maxValue) * chartHeight;
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
    });
}

init();
