package com.persistentagent.api.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import software.amazon.awssdk.core.ResponseInputStream;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.DeleteObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;
import software.amazon.awssdk.services.s3.model.NoSuchKeyException;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;

import jakarta.annotation.PostConstruct;
import java.net.URI;
import java.util.Optional;

@Service
public class S3StorageService {

    private static final Logger logger = LoggerFactory.getLogger(S3StorageService.class);

    @Value("${s3.endpoint-url:#{null}}")
    private String endpointUrl;

    @Value("${s3.bucket-name:platform-artifacts}")
    private String bucketName;

    @Value("${s3.region:us-east-1}")
    private String region;

    private S3Client s3Client;

    @PostConstruct
    void init() {
        var builder = S3Client.builder()
                .region(Region.of(region));

        if (endpointUrl != null && !endpointUrl.isBlank()) {
            builder.endpointOverride(URI.create(endpointUrl))
                   .forcePathStyle(true);
        }

        this.s3Client = builder.build();
        logger.info("S3StorageService initialized: bucket={}, endpoint={}, region={}",
                bucketName, endpointUrl != null ? endpointUrl : "default (AWS)", region);
    }

    public Optional<ResponseInputStream<GetObjectResponse>> download(String s3Key) {
        try {
            GetObjectRequest request = GetObjectRequest.builder()
                    .bucket(bucketName)
                    .key(s3Key)
                    .build();

            ResponseInputStream<GetObjectResponse> response = s3Client.getObject(request);
            logger.info("S3 download started: bucket={}, key={}", bucketName, s3Key);
            return Optional.of(response);
        } catch (NoSuchKeyException e) {
            logger.warn("S3 object not found: bucket={}, key={}", bucketName, s3Key);
            return Optional.empty();
        }
    }

    public void upload(String s3Key, byte[] data, String contentType) {
        PutObjectRequest request = PutObjectRequest.builder()
                .bucket(bucketName)
                .key(s3Key)
                .contentType(contentType)
                .contentLength((long) data.length)
                .build();

        s3Client.putObject(request, RequestBody.fromBytes(data));
        logger.info("S3 upload completed: bucket={}, key={}, size={}", bucketName, s3Key, data.length);
    }

    public void delete(String s3Key) {
        try {
            DeleteObjectRequest request = DeleteObjectRequest.builder()
                    .bucket(bucketName)
                    .key(s3Key)
                    .build();
            s3Client.deleteObject(request);
            logger.info("S3 delete completed: bucket={}, key={}", bucketName, s3Key);
        } catch (Exception e) {
            logger.warn("S3 delete failed (best-effort): bucket={}, key={}, error={}",
                    bucketName, s3Key, e.getMessage());
        }
    }
}
