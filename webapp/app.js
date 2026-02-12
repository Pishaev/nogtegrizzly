// Инициализация Telegram Web App
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

// Запросы идут на тот же домен (Vercel), прокси перенаправляет на бота — так нет CORS
const API_URL = '';


// Состояние приложения
let userData = null;
let eventsData = null;

// Инициализация
async function init() {
    try {
        // Получаем данные пользователя из Telegram
        const initData = tg.initData;
        const initDataUnsafe = tg.initDataUnsafe;
        
        // Загружаем данные пользователя
        await loadUserData();
        await loadEventsData();
        
        // Обновляем интерфейс
        updateMainScreen();
        
        // Настраиваем обработчики
        setupEventHandlers();
    } catch (error) {
        console.error('Ошибка инициализации:', error);
        tg.showAlert('Ошибка загрузки данных. Попробуйте позже.');
    }
}

// Загрузка данных пользователя
async function loadUserData() {
    try {
        // Получаем initData для авторизации
        const initData = tg.initData;
        
        if (!initData) {
            tg.showAlert('Ошибка: initData не доступен. Откройте мини-приложение кнопкой в боте.');
            throw new Error('initData не доступен');
        }
        
        const url = (API_URL || window.location.origin) + '/api/user';
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                initData: initData
            })
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
        if (!msg.includes('Ошибка загрузки:')) {
            tg.showAlert('Ошибка: ' + msg);
        }
        throw error;
    }
}

// Загрузка данных о событиях
async function loadEventsData() {
    try {
        const initData = tg.initData;
        
        const response = await fetch(`${API_URL}/api/events`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                initData: initData
            })
        });
        
        if (!response.ok) {
            throw new Error('Ошибка загрузки событий');
        }
        
        eventsData = await response.json();
    } catch (error) {
        console.error('Ошибка загрузки событий:', error);
        // Не критично, продолжаем работу
        eventsData = { events: [], chartData: [] };
    }
}

// Обновление главного экрана
function updateMainScreen() {
    if (!userData) return;
    
    const userName = userData.name || 'друг';
    const currentStreak = userData.current_streak || 0;
    
    document.getElementById('userName').textContent = userName;
    document.getElementById('currentStreak').textContent = currentStreak;
}

// Обновление экрана статистики
function updateStatsScreen() {
    if (!userData || !eventsData) return;
    
    const daysWithout = userData.current_streak || 0;
    const eventsCount = eventsData.events?.length || 0;
    
    document.getElementById('daysWithout').textContent = daysWithout;
    document.getElementById('eventsCount').textContent = eventsCount;
    
    // Рисуем график
    drawChart(eventsData.chartData || []);
}

// Настройка обработчиков событий
function setupEventHandlers() {
    // Кнопка "Записать момент"
    document.getElementById('recordBtn').addEventListener('click', () => {
        // Открываем бота с командой для записи момента
        tg.openTelegramLink(`https://t.me/nogtegrizzly_bot?start=record`);
    });
    
    // Кнопка "Статистика"
    document.getElementById('statsBtn').addEventListener('click', () => {
        showStatsScreen();
    });
    
    // Кнопка "Назад"
    document.getElementById('backBtn').addEventListener('click', () => {
        showMainScreen();
    });
}

// Показать главный экран
function showMainScreen() {
    document.getElementById('mainScreen').classList.add('active');
    document.getElementById('statsScreen').classList.remove('active');
}

// Показать экран статистики
function showStatsScreen() {
    updateStatsScreen();
    document.getElementById('mainScreen').classList.remove('active');
    document.getElementById('statsScreen').classList.add('active');
}

// Рисование графика
function drawChart(chartData) {
    const canvas = document.getElementById('streakChart');
    const ctx = canvas.getContext('2d');
    
    // Устанавливаем размеры canvas
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * window.devicePixelRatio;
    canvas.height = rect.height * window.devicePixelRatio;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
    
    const width = rect.width;
    const height = rect.height;
    
    // Очищаем canvas
    ctx.clearRect(0, 0, width, height);
    
    if (!chartData || chartData.length === 0) {
        // Если данных нет, показываем сообщение
        ctx.fillStyle = '#999';
        ctx.font = '14px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('Данных пока нет', width / 2, height / 2);
        return;
    }
    
    // Находим максимальное значение
    const maxValue = Math.max(...chartData.map(d => d.value), 1);
    
    // Отступы
    const padding = 20;
    const chartWidth = width - padding * 2;
    const chartHeight = height - padding * 2;
    
    // Рисуем оси
    ctx.strokeStyle = '#ccc';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding, padding);
    ctx.lineTo(padding, height - padding);
    ctx.lineTo(width - padding, height - padding);
    ctx.stroke();
    
    // Рисуем график
    ctx.strokeStyle = '#2481cc';
    ctx.lineWidth = 2;
    ctx.beginPath();
    
    const stepX = chartWidth / Math.max(chartData.length - 1, 1);
    
    chartData.forEach((point, index) => {
        const x = padding + index * stepX;
        const y = height - padding - (point.value / maxValue) * chartHeight;
        
        if (index === 0) {
            ctx.moveTo(x, y);
        } else {
            ctx.lineTo(x, y);
        }
    });
    
    ctx.stroke();
    
    // Рисуем точки
    ctx.fillStyle = '#2481cc';
    chartData.forEach((point, index) => {
        const x = padding + index * stepX;
        const y = height - padding - (point.value / maxValue) * chartHeight;
        
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
    });
}

// Запуск приложения
init();
