package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"

	"mts/internal/models"
	"mts/internal/sse"
	"mts/internal/storage"
)

type Worker struct {
	redis     *storage.RedisStorage
	postgres  *storage.PostgresStorage
	sseBroker *sse.Broker
	mlURL     string
	mlTimeout time.Duration
	stopCh    chan struct{}
}

func NewWorker(
	redis *storage.RedisStorage,
	postgres *storage.PostgresStorage,
	sseBroker *sse.Broker,
	mlURL string,
	mlTimeout time.Duration,
) *Worker {
	return &Worker{
		redis:     redis,
		postgres:  postgres,
		sseBroker: sseBroker,
		mlURL:     mlURL,
		mlTimeout: mlTimeout,
		stopCh:    make(chan struct{}),
	}
}

func (w *Worker) Start(ctx context.Context) {
	log.Println("Worker started")
	for {
		select {
		case <-ctx.Done():
			log.Println("Worker stopping due to context")
			return
		case <-w.stopCh:
			log.Println("Worker stopping")
			return
		default:
			taskID, taskText, err := w.redis.PopTask(ctx)
			if err != nil {
				time.Sleep(1 * time.Second)
				continue
			}

			log.Printf("Worker processing task: %s", taskID)
			w.processTask(ctx, taskID, taskText)
		}
	}
}

func (w *Worker) Stop() {
	close(w.stopCh)
}

func (w *Worker) processTask(ctx context.Context, taskID, taskText string) {
	// Обновляем статус
	w.postgres.UpdateQueryStatus(ctx, taskID, "processing")
	w.sseBroker.SendEvent(taskID, `{"step":"started","status":"processing","detail":"Task started"}`)

	// Вызываем ML сервис с retry
	var result *models.ResultPayload
	var err error

	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			log.Printf("Retry %d for task %s", attempt, taskID)
			time.Sleep(time.Duration(attempt) * 2 * time.Second)
		}
		result, err = w.callMLService(ctx, taskID, taskText)
		if err == nil {
			break
		}
	}

	if err != nil {
		log.Printf("ML service failed for task %s: %v", taskID, err)
		result = &models.ResultPayload{
			Status: "failed",
			Error:  fmt.Sprintf("ML service error: %v", err),
		}
		w.sseBroker.SendEvent(taskID, `{"step":"error","status":"failed","detail":"`+err.Error()+`"}`)
	}

	// Сохраняем результат
	if err := w.postgres.SaveResult(ctx, taskID, result); err != nil {
		log.Printf("Failed to save result for task %s: %v", taskID, err)
	}

	// Кэшируем в Redis (опционально)
	// queryHash := generateHash(taskText)
	// w.redis.CacheResult(ctx, queryHash, result)

	w.sseBroker.SendEvent(taskID, `{"step":"completed","status":"done","detail":"Task finished"}`)
	log.Printf("Task %s completed", taskID)
}

func (w *Worker) callMLService(ctx context.Context, taskID, taskText string) (*models.ResultPayload, error) {
	reqBody := models.MLRequest{
		QueryID: taskID,
		Text:    taskText,
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, err
	}

	ctx, cancel := context.WithTimeout(ctx, w.mlTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", w.mlURL+"/process", bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: w.mlTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("ML service returned %d: %s", resp.StatusCode, string(body))
	}

	var result models.ResultPayload
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}

	return &result, nil
}
