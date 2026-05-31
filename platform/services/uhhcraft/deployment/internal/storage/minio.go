// Package storage wraps MinIO (S3-compatible) for asset management.
package storage

import (
	"context"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"

	appconfig "github.com/wisward/uhhcraft/internal/config"
)

// CanvasURLTTL is how long presigned asset URLs for the 3D canvas are valid.
const CanvasURLTTL = 15 * time.Minute

// Client wraps the AWS S3 client pointed at MinIO.
type Client struct {
	s3     *s3.Client
	bucket string
}

// New creates a MinIO-compatible S3 client from the app config.
func New(cfg *appconfig.StorageConfig) (*Client, error) {
	scheme := "http"
	if cfg.UseSSL {
		scheme = "https"
	}
	endpoint := fmt.Sprintf("%s://%s", scheme, cfg.Endpoint)

	awsCfg, err := config.LoadDefaultConfig(context.Background(),
		config.WithCredentialsProvider(credentials.NewStaticCredentialsProvider(
			cfg.AccessKey, cfg.SecretKey, "",
		)),
		config.WithRegion("us-east-1"),
	)
	if err != nil {
		return nil, fmt.Errorf("storage config: %w", err)
	}

	return &Client{
		s3: s3.NewFromConfig(awsCfg, func(o *s3.Options) {
			o.UsePathStyle = true
			o.BaseEndpoint = aws.String(endpoint)
		}),
		bucket: cfg.Bucket,
	}, nil
}

// Put uploads data to the given key in the default bucket.
func (c *Client) Put(ctx context.Context, key, contentType string, body io.Reader, size int64) error {
	_, err := c.s3.PutObject(ctx, &s3.PutObjectInput{
		Bucket:        aws.String(c.bucket),
		Key:           aws.String(key),
		Body:          body,
		ContentType:   aws.String(contentType),
		ContentLength: aws.Int64(size),
	})
	return err
}

// Get downloads the object at key and returns an io.ReadCloser.
// Caller must close the body.
func (c *Client) Get(ctx context.Context, key string) (io.ReadCloser, int64, error) {
	out, err := c.s3.GetObject(ctx, &s3.GetObjectInput{
		Bucket: aws.String(c.bucket),
		Key:    aws.String(key),
	})
	if err != nil {
		return nil, 0, err
	}
	size := int64(0)
	if out.ContentLength != nil {
		size = *out.ContentLength
	}
	return out.Body, size, nil
}

// Delete removes an object from the bucket.
func (c *Client) Delete(ctx context.Context, key string) error {
	_, err := c.s3.DeleteObject(ctx, &s3.DeleteObjectInput{
		Bucket: aws.String(c.bucket),
		Key:    aws.String(key),
	})
	return err
}

// PresignGet returns a presigned GET URL for the given key, valid for the given duration.
func (c *Client) PresignGet(ctx context.Context, key string, ttl time.Duration) (string, error) {
	presign := s3.NewPresignClient(c.s3)
	req, err := presign.PresignGetObject(ctx, &s3.GetObjectInput{
		Bucket: aws.String(c.bucket),
		Key:    aws.String(key),
	}, s3.WithPresignExpires(ttl))
	if err != nil {
		return "", fmt.Errorf("presign %s: %w", key, err)
	}
	return req.URL, nil
}

// EnsureBucket creates the bucket if it doesn't already exist.
func (c *Client) EnsureBucket(ctx context.Context) error {
	_, err := c.s3.CreateBucket(ctx, &s3.CreateBucketInput{
		Bucket: aws.String(c.bucket),
	})
	if err != nil && !isBucketAlreadyExists(err) {
		return fmt.Errorf("ensure bucket: %w", err)
	}
	return nil
}

// isBucketAlreadyExists reports whether a CreateBucket error means the bucket
// already exists (owned by us or by someone else). It matches the typed AWS
// SDK error structs via errors.As rather than fragile substring matching on
// the error text, which can change across SDK/server versions.
func isBucketAlreadyExists(err error) bool {
	if err == nil {
		return false
	}
	var owned *s3types.BucketAlreadyOwnedByYou
	var exists *s3types.BucketAlreadyExists
	return errors.As(err, &owned) || errors.As(err, &exists)
}
