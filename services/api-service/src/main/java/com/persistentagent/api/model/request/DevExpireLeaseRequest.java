package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;

public record DevExpireLeaseRequest(
        @JsonProperty("lease_owner") String leaseOwner
) {
}
