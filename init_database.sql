-- Создание базы данных (если не существует)
CREATE DATABASE IF NOT EXISTS defaultdb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE defaultdb;

-- Таблица привычек
CREATE TABLE IF NOT EXISTS habits (
    id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Таблица статистики
CREATE TABLE IF NOT EXISTS stats (
    id INT AUTO_INCREMENT PRIMARY KEY,
    date VARCHAR(8) NOT NULL,
    habit_id VARCHAR(50) NOT NULL,
    status TINYINT(1) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_date_habit (date, habit_id),
    FOREIGN KEY (habit_id) REFERENCES habits(id) ON DELETE CASCADE
);

-- Индексы для быстрого поиска
CREATE INDEX idx_stats_date ON stats(date);
CREATE INDEX idx_stats_habit_id ON stats(habit_id);