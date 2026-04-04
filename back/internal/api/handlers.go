package api

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"mts/internal/models"
	"mts/internal/sse"
	"mts/internal/storage"
)

type Handlers struct {
	postgres  *storage.PostgresStorage
	redis     *storage.RedisStorage
	sseBroker *sse.Broker
}

func NewHandlers(pg *storage.PostgresStorage, rd *storage.RedisStorage, sse *sse.Broker) *Handlers {
	return &Handlers{
		postgres:  pg,
		redis:     rd,
		sseBroker: sse,
	}
}

type QueryRequest struct {
	Query string `json:"query"`
}

type QueryResponse struct {
	RequestID string `json:"request_id"`
}

func (h *Handlers) HandleQuery(w http.ResponseWriter, r *http.Request) {
	var req QueryRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	if req.Query == "" {
		http.Error(w, "Query cannot be empty", http.StatusBadRequest)
		return
	}

	// Генерируем ID
	requestID := uuid.New().String()

	// Сохраняем в PostgreSQL
	ctx := r.Context()
	if err := h.postgres.CreateQuery(ctx, requestID, req.Query, "pending"); err != nil {
		log.Printf("Failed to create query: %v", err)
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}

	// Отправляем в очередь Redis
	if err := h.redis.PushTask(ctx, requestID, req.Query); err != nil {
		log.Printf("Failed to push task to Redis: %v", err)
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(QueryResponse{RequestID: requestID})
}

func (h *Handlers) HandleResult(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	if id == "" {
		http.Error(w, "ID is required", http.StatusBadRequest)
		return
	}

	ctx := r.Context()

	// Сначала пробуем получить результат
	result, err := h.postgres.GetResult(ctx, id)
	if err != nil {
		log.Printf("Failed to get result: %v", err)
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}

	if result == nil {
		// Результата еще нет, возвращаем статус
		query, err := h.postgres.GetQuery(ctx, id)
		if err != nil {
			http.Error(w, "Internal server error", http.StatusInternalServerError)
			return
		}
		if query == nil {
			http.Error(w, "Query not found", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"status": query.Status,
			"id":     id,
		})
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(result)
}

func (h *Handlers) HandleSSE(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	if id == "" {
		http.Error(w, "ID is required", http.StatusBadRequest)
		return
	}

	// Проверяем, существует ли запрос
	ctx := r.Context()
	query, err := h.postgres.GetQuery(ctx, id)
	if err != nil || query == nil {
		http.Error(w, "Query not found", http.StatusNotFound)
		return
	}

	// Настройка SSE
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("Access-Control-Allow-Origin", "*")

	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "SSE not supported", http.StatusInternalServerError)
		return
	}

	// Подписываемся на события
	eventCh := h.sseBroker.Subscribe(id)
	defer h.sseBroker.Unsubscribe(id)

	// Отправляем начальное событие
	fmt.Fprintf(w, "data: {\"step\":\"connected\",\"status\":\"ok\"}\n\n")
	flusher.Flush()

	// Ждем события
	for {
		select {
		case event, ok := <-eventCh:
			if !ok {
				return
			}
			fmt.Fprintf(w, "data: %s\n\n", event)
			flusher.Flush()

		case <-r.Context().Done():
			return
		}
	}
}

func (h *Handlers) HandleHistory(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	queries, err := h.postgres.GetHistory(ctx, 10)
	if err != nil {
		log.Printf("Failed to get history: %v", err)
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(queries)
}

func (h *Handlers) HandleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]string{
		"status": "ok",
		"time":   time.Now().Format(time.RFC3339),
	})
}

func (h *Handlers) HandleInternalTrace(w http.ResponseWriter, r *http.Request) {
	var trace models.MLTrace
	if err := json.NewDecoder(r.Body).Decode(&trace); err != nil {
		http.Error(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	// Преобразуем в JSON строку для SSE
	eventData, _ := json.Marshal(trace.Event)
	h.sseBroker.SendEvent(trace.QueryID, string(eventData))

	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}
