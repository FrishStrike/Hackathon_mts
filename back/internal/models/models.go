package models

import (
	"time"
)

type Query struct {
	ID        string    `json:"id"`
	Text      string    `json:"text"`
	Status    string    `json:"status"` // pending, processing, completed, failed
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

type Result struct {
	ID      string        `json:"id"`
	Payload ResultPayload `json:"payload"`
}

type ResultPayload struct {
	Status  string   `json:"status"` // completed, failed
	Item    *Item    `json:"item,omitempty"`
	News    []News   `json:"news,omitempty"`
	Trace   []string `json:"trace,omitempty"`
	Sources []string `json:"sources,omitempty"`
	Error   string   `json:"error,omitempty"`
}

type Item struct {
	Title string            `json:"title"`
	Price string            `json:"price"`
	URL   string            `json:"url"`
	Specs map[string]string `json:"specs"`
}

type News struct {
	Title   string `json:"title"`
	Date    string `json:"date"`
	URL     string `json:"url"`
	Summary string `json:"summary"`
}

type TraceEvent struct {
	Step   string `json:"step"`
	Status string `json:"status"` // started, in_progress, done, error
	Detail string `json:"detail,omitempty"`
}

type MLRequest struct {
	QueryID string `json:"query_id"`
	Text    string `json:"text"`
}

type MLTrace struct {
	QueryID string     `json:"query_id"`
	Event   TraceEvent `json:"event"`
}
