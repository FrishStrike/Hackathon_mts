package storage

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"

	"github.com/redis/go-redis/v9"

	"mts/internal/models"
)

type RedisStorage struct {
	client *redis.Client
	ttl    time.Duration
}

func NewRedisStorage(host, port, password string, ttlMinutes int) (*RedisStorage, error) {
	client := redis.NewClient(&redis.Options{
		Addr:     fmt.Sprintf("%s:%s", host, port),
		Password: password,
		DB:       0,
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := client.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("failed to connect to Redis: %w", err)
	}

	log.Println("Redis connected")
	return &RedisStorage{
		client: client,
		ttl:    time.Duration(ttlMinutes) * time.Minute,
	}, nil
}

func (s *RedisStorage) CacheResult(ctx context.Context, queryHash string, result *models.ResultPayload) error {
	data, err := json.Marshal(result)
	if err != nil {
		return err
	}
	return s.client.Set(ctx, queryHash, data, s.ttl).Err()
}

func (s *RedisStorage) GetCachedResult(ctx context.Context, queryHash string) (*models.ResultPayload, error) {
	data, err := s.client.Get(ctx, queryHash).Bytes()
	if err == redis.Nil {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	var result models.ResultPayload
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, err
	}
	return &result, nil
}

// Для очереди задач
func (s *RedisStorage) PushTask(ctx context.Context, taskID, queryText string) error {
	task := map[string]string{
		"id":   taskID,
		"text": queryText,
	}
	data, err := json.Marshal(task)
	if err != nil {
		return err
	}
	return s.client.LPush(ctx, "task_queue", data).Err()
}

func (s *RedisStorage) PopTask(ctx context.Context) (string, string, error) {
	result, err := s.client.BRPop(ctx, 5*time.Second, "task_queue").Result()
	if err != nil {
		return "", "", err
	}
	if len(result) < 2 {
		return "", "", fmt.Errorf("invalid queue result")
	}

	var task struct {
		ID   string `json:"id"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal([]byte(result[1]), &task); err != nil {
		return "", "", err
	}
	return task.ID, task.Text, nil
}

func (s *RedisStorage) Close() error {
	return s.client.Close()
}
