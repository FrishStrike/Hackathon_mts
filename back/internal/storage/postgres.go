package storage

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"mts/internal/models"
)

type PostgresStorage struct {
	pool *pgxpool.Pool
}

func NewPostgresStorage(host, port, user, password, dbname string) (*PostgresStorage, error) {
	connString := fmt.Sprintf(
		"postgres://%s:%s@%s:%s/%s?sslmode=disable",
		user, password, host, port, dbname,
	)

	config, err := pgxpool.ParseConfig(connString)
	if err != nil {
		return nil, fmt.Errorf("failed to parse config: %w", err)
	}

	pool, err := pgxpool.NewWithConfig(context.Background(), config)
	if err != nil {
		return nil, fmt.Errorf("failed to create pool: %w", err)
	}

	// Проверка подключения
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("failed to ping database: %w", err)
	}

	// Создание таблиц
	if err := createTables(pool); err != nil {
		return nil, fmt.Errorf("failed to create tables: %w", err)
	}

	log.Println("PostgreSQL connected")
	return &PostgresStorage{pool: pool}, nil
}

func createTables(pool *pgxpool.Pool) error {
	queries := []string{
		`CREATE TABLE IF NOT EXISTS queries (
			id VARCHAR(36) PRIMARY KEY,
			text TEXT NOT NULL,
			status VARCHAR(20) NOT NULL DEFAULT 'pending',
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		)`,
		`CREATE TABLE IF NOT EXISTS results (
			id VARCHAR(36) PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
			payload JSONB NOT NULL,
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		)`,
		`CREATE INDEX IF NOT EXISTS idx_queries_status ON queries(status)`,
		`CREATE INDEX IF NOT EXISTS idx_queries_created_at ON queries(created_at DESC)`,
	}

	ctx := context.Background()
	for _, query := range queries {
		if _, err := pool.Exec(ctx, query); err != nil {
			return err
		}
	}
	return nil
}

func (s *PostgresStorage) CreateQuery(ctx context.Context, id, text, status string) error {
	_, err := s.pool.Exec(ctx,
		"INSERT INTO queries (id, text, status) VALUES ($1, $2, $3)",
		id, text, status,
	)
	return err
}

func (s *PostgresStorage) UpdateQueryStatus(ctx context.Context, id, status string) error {
	_, err := s.pool.Exec(ctx,
		"UPDATE queries SET status = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
		status, id,
	)
	return err
}

func (s *PostgresStorage) GetQuery(ctx context.Context, id string) (*models.Query, error) {
	var q models.Query
	err := s.pool.QueryRow(ctx,
		"SELECT id, text, status, created_at, updated_at FROM queries WHERE id = $1",
		id,
	).Scan(&q.ID, &q.Text, &q.Status, &q.CreatedAt, &q.UpdatedAt)
	if err == pgx.ErrNoRows {
		return nil, nil
	}
	return &q, err
}

func (s *PostgresStorage) SaveResult(ctx context.Context, id string, payload *models.ResultPayload) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	// Обновляем статус
	_, err = tx.Exec(ctx,
		"UPDATE queries SET status = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
		payload.Status, id,
	)
	if err != nil {
		return err
	}

	// Сохраняем результат
	_, err = tx.Exec(ctx,
		`INSERT INTO results (id, payload) VALUES ($1, $2)
		 ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = CURRENT_TIMESTAMP`,
		id, payload,
	)
	if err != nil {
		return err
	}

	return tx.Commit(ctx)
}

func (s *PostgresStorage) GetResult(ctx context.Context, id string) (*models.ResultPayload, error) {
	var payload models.ResultPayload
	err := s.pool.QueryRow(ctx,
		"SELECT payload FROM results WHERE id = $1",
		id,
	).Scan(&payload)
	if err == pgx.ErrNoRows {
		return nil, nil
	}
	return &payload, err
}

func (s *PostgresStorage) GetHistory(ctx context.Context, limit int) ([]models.Query, error) {
	rows, err := s.pool.Query(ctx,
		"SELECT id, text, status, created_at, updated_at FROM queries ORDER BY created_at DESC LIMIT $1",
		limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var queries []models.Query
	for rows.Next() {
		var q models.Query
		if err := rows.Scan(&q.ID, &q.Text, &q.Status, &q.CreatedAt, &q.UpdatedAt); err != nil {
			return nil, err
		}
		queries = append(queries, q)
	}
	return queries, nil
}

func (s *PostgresStorage) Close() {
	s.pool.Close()
}
