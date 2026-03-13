package com.persistentagent.api.controller;

import com.persistentagent.api.model.response.ModelInfo;
import com.persistentagent.api.repository.ModelRepository;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/v1/models")
public class ModelController {

    private final ModelRepository modelRepository;

    public ModelController(ModelRepository modelRepository) {
        this.modelRepository = modelRepository;
    }

    @GetMapping
    public List<ModelInfo> getModels() {
        return modelRepository.findActiveModels();
    }
}
