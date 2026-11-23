/**
 * The base model library — 85 real model architectures, the single source of
 * truth the seed loads into \`base_models\`.
 *
 * Beyond the descriptive fields, each entry carries the metadata the catalog
 * and the Train wizard actually need to filter and configure it:
 *
 *  - \`category\`         coarse grouping for classical-ML entries
 *                       (classification / regression / clustering /
 *                       anomaly_detection / timeseries /
 *                       dimensionality_reduction). Null for LLM entries,
 *                       which are grouped by \`task\` instead.
 *  - \`algorithmKey\`     the trainer implementation to dispatch to.
 *  - \`algorithmVariant\` disambiguates entries sharing one algorithmKey.
 *  - \`params\`/\`context\` published model-card facts for LLM entries
 *                       (parameter count, context window). Null where the
 *                       model has no meaningful published figure — never
 *                       guessed.
 *  - \`alternatives\`     sibling models worth considering instead.
 *  - \`defaultHp\`        the hyperparameters training actually starts from.
 *  - \`supportedHp\`      the tunable surface (type, default, bounds/values)
 *                       that drives HPO search spaces and wizard validation.
 *
 * \`defaultHp\`/\`supportedHp\` are present only where a real, documented
 * default set exists for that trainer. A null here means "not specified for
 * this entry" and the UI says so, rather than inventing a plausible-looking
 * hyperparameter block for a model whose trainer never defined one.
 */
export const ALL_BASE_MODELS = [
  {
    "id": "xgboost-classifier",
    "domain": "ml",
    "title": "xgboost-classifier",
    "task": "Classification / regression",
    "description": "Gradient boosting — classification & regression",
    "framework": "xgboost",
    "license": "Apache 2.0",
    "uri": "oci://registry.inaiera.io/mlops/trainer-xgboost:1.4",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "General-purpose gradient-boosted trees. The default pick for structured/tabular classification and regression tasks — strong accuracy, fast training, well understood failure modes. Runs as a Ray Train remote task, scales from a single worker to distributed training on the same script.",
    "category": "classification",
    "algorithmKey": "xgboost",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "lightgbm-ranker",
      "random-forest"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "lightgbm-ranker",
    "domain": "ml",
    "title": "lightgbm-ranker",
    "task": "Ranking",
    "description": "Fast gradient boosting — ranking tasks",
    "framework": "lightgbm",
    "license": "MIT",
    "uri": "oci://registry.inaiera.io/mlops/trainer-lightgbm:1.2",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Histogram-based gradient boosting, tuned for ranking and large-cardinality categorical features. Faster than xgboost on wide tabular data at a small accuracy trade-off.",
    "category": "classification",
    "algorithmKey": "lightgbm",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-classifier"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "random-forest",
    "domain": "ml",
    "title": "random-forest",
    "task": "Classification / regression",
    "description": "Ensemble trees — classification & regression",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.ensemble.RandomForestClassifier",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Bagged decision trees — the safest default when interpretability and low variance matter more than squeezing out the last few points of accuracy. Minimal hyperparameter sensitivity.",
    "category": "classification",
    "algorithmKey": "random_forest",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-classifier"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "prophet-forecaster",
    "domain": "ml",
    "title": "prophet-forecaster",
    "task": "Forecasting",
    "description": "Time-series forecasting",
    "framework": "prophet",
    "license": "MIT",
    "uri": "oci://registry.inaiera.io/mlops/trainer-prophet:1.0",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Additive time-series model built for business forecasting — handles seasonality, holidays, and missing data without much manual tuning. Not a deep-learning forecaster; use tabular-embedding-nn if you need one.",
    "category": "timeseries",
    "algorithmKey": "prophet",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [],
    "defaultHp": {
      "seasonality_mode": "additive",
      "changepoint_prior_scale": 0.05
    },
    "supportedHp": {
      "seasonality_mode": {
        "type": "enum",
        "default": "additive",
        "values": [
          "additive",
          "multiplicative"
        ]
      },
      "changepoint_prior_scale": {
        "type": "float",
        "default": 0.05,
        "min": 0.001,
        "max": 0.5
      },
      "weekly_seasonality": {
        "type": "bool",
        "default": true
      }
    }
  },
  {
    "id": "tabular-embedding-nn",
    "domain": "ml",
    "title": "tabular-embedding-nn",
    "task": "Embedding",
    "description": "PyTorch — tabular embeddings",
    "framework": "pytorch",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-tabular:2.1",
    "gpu": false,
    "computeProfile": "cpu-large",
    "longDescription": "A small feed-forward network that learns dense embeddings of tabular rows — useful for similarity search or as a feature-engineering step ahead of a simpler downstream model. CPU-trainable for typical dataset sizes; scale to gpu-1x for very large tables.",
    "category": "dimensionality_reduction",
    "algorithmKey": null,
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [],
    "defaultHp": {
      "embedding_dim": 64,
      "epochs": 10,
      "learning_rate": 0.001
    },
    "supportedHp": {
      "embedding_dim": {
        "type": "int",
        "default": 64,
        "min": 8,
        "max": 512
      },
      "epochs": {
        "type": "int",
        "default": 10,
        "min": 1,
        "max": 200
      },
      "learning_rate": {
        "type": "float",
        "default": 0.001,
        "min": 0.00001,
        "max": 0.1
      }
    }
  },
  {
    "id": "llama-3-8b-instruct",
    "domain": "llm",
    "title": "llama-3-8b-instruct",
    "task": "Chat",
    "description": "Reasoning / agentic · Apache 2.0",
    "framework": "huggingface",
    "license": "Apache 2.0",
    "uri": "hf://meta-llama/Meta-Llama-3-8B-Instruct",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "The default shared base for LoRA fine-tuning in this deployment — two adapters (support-tone-adapter, legal-summarizer-adapter) already serve off one shared vLLM deployment against this base, rather than each getting its own GPU. Good general instruction-following; not the strongest agentic/coding model in the library (see glm-5.2).",
    "category": null,
    "algorithmKey": null,
    "algorithmVariant": null,
    "params": "8B",
    "context": "128K",
    "alternatives": [
      "glm-5.2",
      "gpt-oss-20b",
      "qwen3-27b"
    ],
    "defaultHp": {
      "temperature": 0.7,
      "max_tokens": 512,
      "top_p": 0.9
    },
    "supportedHp": {
      "temperature": {
        "type": "float",
        "default": 0.7,
        "min": 0,
        "max": 2
      },
      "max_tokens": {
        "type": "int",
        "default": 512,
        "min": 1,
        "max": 8192
      },
      "top_p": {
        "type": "float",
        "default": 0.9,
        "min": 0,
        "max": 1
      },
      "lora": {
        "type": "enum",
        "default": "r=16",
        "values": [
          "r=8",
          "r=16",
          "r=32"
        ]
      }
    }
  },
  {
    "id": "qwen2.5-coder-1.5b-instruct",
    "domain": "llm",
    "title": "qwen2.5-coder-1.5b-instruct",
    "task": "Code generation",
    "description": "Code-specialised chat model for SQL / dbt / Flink fine-tuning · Apache 2.0",
    "framework": "huggingface",
    "license": "Apache 2.0",
    "uri": "hf://Qwen/Qwen2.5-Coder-1.5B-Instruct",
    "gpu": false,
    "computeProfile": "cpu-large",
    "longDescription": "Qwen2.5-Coder 1.5B instruct: pretrained mostly on source code, so it starts from far better SQL and YAML than a general model of the same size. ~3 GB, ungated. LoRA fine-tunes on Apple-silicon (MPS) or CPU; QLoRA needs CUDA.",
    "category": null,
    "algorithmKey": null,
    "algorithmVariant": null,
    "params": "1.5B",
    "context": "32K",
    "alternatives": [
      "qwen2.5-0.5b-instruct"
    ],
    "defaultHp": {
      "temperature": 0.2,
      "max_tokens": 512,
      "top_p": 0.9
    },
    "supportedHp": {
      "temperature": { "type": "float", "default": 0.2, "min": 0, "max": 2 },
      "max_tokens": { "type": "int", "default": 512, "min": 1, "max": 8192 },
      "top_p": { "type": "float", "default": 0.9, "min": 0, "max": 1 },
      "lora": { "type": "enum", "default": "r=16", "values": ["r=8", "r=16", "r=32"] }
    }
  },
  {
    "id": "qwen2.5-0.5b-instruct",
    "domain": "llm",
    "title": "qwen2.5-0.5b-instruct",
    "task": "Chat / instruction following",
    "description": "Small general chat model for laptop LoRA fine-tuning · Apache 2.0",
    "framework": "huggingface",
    "license": "Apache 2.0",
    "uri": "hf://Qwen/Qwen2.5-0.5B-Instruct",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Qwen2.5 0.5B instruct: a general chat model small enough to LoRA fine-tune and serve on a laptop CPU (~1 GB download, ~2 GB in memory). Ungated. Use it to teach a tone, a format or a domain's answers from a prompt/completion dataset; for code, start from qwen2.5-coder-1.5b-instruct.",
    "category": null,
    "algorithmKey": null,
    "algorithmVariant": null,
    "params": "0.5B",
    "context": "32K",
    "alternatives": [
      "qwen2.5-coder-1.5b-instruct"
    ],
    "defaultHp": {
      "temperature": 0.3,
      "max_tokens": 512,
      "top_p": 0.9
    },
    "supportedHp": {
      "temperature": { "type": "float", "default": 0.3, "min": 0, "max": 2 },
      "max_tokens": { "type": "int", "default": 512, "min": 1, "max": 8192 },
      "top_p": { "type": "float", "default": 0.9, "min": 0, "max": 1 },
      "lora": { "type": "enum", "default": "r=16", "values": ["r=8", "r=16", "r=32"] }
    }
  },
  {
    "id": "glm-5.2",
    "domain": "llm",
    "title": "glm-5.2",
    "task": "Chat / agentic",
    "description": "Reasoning / agentic · MIT",
    "framework": "huggingface",
    "license": "MIT",
    "uri": "hf://zai-org/GLM-5.2",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "The recommended default for agentic/coding tasks and anything sharing a model across multiple InAiEra products — MIT is the cleanest license in the library for that kind of reuse. Mixture-of-experts, so active-parameter footprint at inference is much smaller than the raw parameter count suggests.",
    "category": null,
    "algorithmKey": null,
    "algorithmVariant": null,
    "params": "744B (40B active, MoE)",
    "context": "1M",
    "alternatives": [
      "llama-3-8b-instruct",
      "gpt-oss-20b",
      "qwen3-27b"
    ],
    "defaultHp": {
      "temperature": 0.6,
      "max_tokens": 1024,
      "top_p": 0.95
    },
    "supportedHp": {
      "temperature": {
        "type": "float",
        "default": 0.6,
        "min": 0,
        "max": 2
      },
      "max_tokens": {
        "type": "int",
        "default": 1024,
        "min": 1,
        "max": 32768
      },
      "top_p": {
        "type": "float",
        "default": 0.95,
        "min": 0,
        "max": 1
      },
      "lora": {
        "type": "enum",
        "default": "r=16",
        "values": [
          "r=8",
          "r=16",
          "r=32"
        ]
      }
    }
  },
  {
    "id": "qwen3-27b",
    "domain": "llm",
    "title": "qwen3-27b",
    "task": "Chat / agentic",
    "description": "Large dense reasoning model · Apache 2.0",
    "framework": "huggingface",
    "license": "Apache 2.0",
    "uri": "hf://Qwen/Qwen3.6-27B",
    "gpu": true,
    "computeProfile": "gpu-4x",
    "longDescription": "A dense (non-MoE) reasoning and agentic model — strong on coding and multi-step tool-use benchmarks, and a more predictable memory footprint than glm-5.2's MoE architecture since every parameter is active on every token. Needs more GPU headroom than the 8B/20B options in the library (gpu-4x, not gpu-1x) — reach for llama-3-8b-instruct or gpt-oss-20b first unless the extra reasoning quality is worth the compute.",
    "category": null,
    "algorithmKey": null,
    "algorithmVariant": null,
    "params": "27B",
    "context": "128K",
    "alternatives": [
      "glm-5.2",
      "llama-3-8b-instruct"
    ],
    "defaultHp": {
      "temperature": 0.6,
      "max_tokens": 1024,
      "top_p": 0.9
    },
    "supportedHp": {
      "temperature": {
        "type": "float",
        "default": 0.6,
        "min": 0,
        "max": 2
      },
      "max_tokens": {
        "type": "int",
        "default": 1024,
        "min": 1,
        "max": 16384
      },
      "top_p": {
        "type": "float",
        "default": 0.9,
        "min": 0,
        "max": 1
      },
      "lora": {
        "type": "enum",
        "default": "r=16",
        "values": [
          "r=8",
          "r=16",
          "r=32"
        ]
      }
    }
  },
  {
    "id": "gpt-oss-20b",
    "domain": "llm",
    "title": "gpt-oss-20b",
    "task": "Chat",
    "description": "Small / edge · fits 16GB · Apache 2.0",
    "framework": "huggingface",
    "license": "Apache 2.0 + usage policy",
    "uri": "hf://openai/gpt-oss-20b",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "The pick when hardware is the binding constraint — fits a single 16GB GPU. A tier behind glm-5.2/llama-3-8b-instruct on coding benchmarks, but the most predictable capacity-planning story of the three.",
    "category": null,
    "algorithmKey": null,
    "algorithmVariant": null,
    "params": "21B",
    "context": "128K",
    "alternatives": [
      "llama-3-8b-instruct",
      "glm-5.2",
      "qwen3-27b"
    ],
    "defaultHp": {
      "temperature": 0.7,
      "max_tokens": 512,
      "top_p": 0.9
    },
    "supportedHp": {
      "temperature": {
        "type": "float",
        "default": 0.7,
        "min": 0,
        "max": 2
      },
      "max_tokens": {
        "type": "int",
        "default": 512,
        "min": 1,
        "max": 8192
      },
      "top_p": {
        "type": "float",
        "default": 0.9,
        "min": 0,
        "max": 1
      },
      "lora": {
        "type": "enum",
        "default": "r=16",
        "values": [
          "r=8",
          "r=16",
          "r=32"
        ]
      }
    }
  },
  {
    "id": "qwen3-embedding-4b",
    "domain": "llm",
    "title": "qwen3-embedding-4b",
    "task": "Embedding",
    "description": "Embedding · tops open MTEB · Apache 2.0",
    "framework": "huggingface",
    "license": "Apache 2.0",
    "uri": "hf://Qwen/Qwen3-Embedding-4B",
    "gpu": true,
    "computeProfile": "gpu-1x (CPU-viable for low volume)",
    "longDescription": "Default embedding model — currently tops the open MTEB multilingual leaderboard. Also available as 0.6B/8B sizes for trading quality against latency/cost; this deployment standardizes on 4B.",
    "category": null,
    "algorithmKey": null,
    "algorithmVariant": null,
    "params": "4B",
    "context": "32K",
    "alternatives": [],
    "defaultHp": {
      "normalize": true,
      "dimensions": 2560
    },
    "supportedHp": {
      "normalize": {
        "type": "bool",
        "default": true
      },
      "dimensions": {
        "type": "int",
        "default": 2560,
        "min": 256,
        "max": 2560
      }
    }
  },
  {
    "id": "whisper-large-v3-turbo",
    "domain": "llm",
    "title": "whisper-large-v3-turbo",
    "task": "Speech-to-text",
    "description": "Speech-to-text · MIT",
    "framework": "huggingface",
    "license": "MIT",
    "uri": "hf://openai/whisper-large-v3-turbo",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "The broad default for transcription — 99+ languages, the most battle-tested ASR model in the library. Use Parakeet instead for real-time/streaming needs where latency matters more than raw accuracy.",
    "category": null,
    "algorithmKey": null,
    "algorithmVariant": null,
    "params": "809M",
    "context": "30s audio chunks",
    "alternatives": [],
    "defaultHp": {
      "language": "auto",
      "task": "transcribe"
    },
    "supportedHp": {
      "language": {
        "type": "string",
        "default": "auto"
      },
      "task": {
        "type": "enum",
        "default": "transcribe",
        "values": [
          "transcribe",
          "translate"
        ]
      }
    }
  },
  {
    "id": "kokoro-82m",
    "domain": "llm",
    "title": "kokoro-82m",
    "task": "Text-to-speech",
    "description": "Text-to-speech · 82M params · Apache 2.0",
    "framework": "huggingface",
    "license": "Apache 2.0",
    "uri": "hf://hexgrad/Kokoro-82M",
    "gpu": false,
    "computeProfile": "cpu-small (GPU not required)",
    "longDescription": "Cheap default for high-volume, low-differentiation narration and notifications — small enough to run on CPU. Use Chatterbox-Turbo instead when voice cloning or a distinctive agent voice matters more than cost.",
    "category": null,
    "algorithmKey": null,
    "algorithmVariant": null,
    "params": "82M",
    "context": "n/a",
    "alternatives": [],
    "defaultHp": {
      "voice": "default",
      "speed": 1
    },
    "supportedHp": {
      "voice": {
        "type": "enum",
        "default": "default",
        "values": [
          "default",
          "warm",
          "narrator"
        ]
      },
      "speed": {
        "type": "float",
        "default": 1,
        "min": 0.5,
        "max": 2
      }
    }
  },
  {
    "id": "extra-trees-classifier",
    "domain": "ml",
    "title": "extra-trees-classifier",
    "task": "Classification",
    "description": "Randomized ensemble trees — classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.ensemble.ExtraTreesClassifier",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Like Random Forest, but splits are chosen at random rather than searched for — usually faster to train with a little extra variance reduction, at a small accuracy cost.",
    "category": "classification",
    "algorithmKey": "extra_trees",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "random-forest"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "gradient-boosting-classifier",
    "domain": "ml",
    "title": "gradient-boosting-classifier",
    "task": "Classification",
    "description": "sklearn's native gradient boosting — classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.ensemble.GradientBoostingClassifier",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "sklearn's own boosting implementation — slower than XGBoost/LightGBM on large data but a dependency-light option when you're already all-in on sklearn.",
    "category": "classification",
    "algorithmKey": "gradient_boosting",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-classifier",
      "lightgbm-ranker"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "histgradientboosting-classifier",
    "domain": "ml",
    "title": "histgradientboosting-classifier",
    "task": "Classification",
    "description": "Histogram-binned boosting — classification, large tabular data",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.ensemble.HistGradientBoostingClassifier",
    "gpu": false,
    "computeProfile": "cpu-large",
    "longDescription": "sklearn's LightGBM-inspired histogram-based booster — much faster than plain GradientBoostingClassifier on large datasets, still a pure-sklearn dependency.",
    "category": "classification",
    "algorithmKey": "histgradientboosting",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "lightgbm-ranker",
      "xgboost-classifier"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "catboost-classifier",
    "domain": "ml",
    "title": "catboost-classifier",
    "task": "Classification",
    "description": "Gradient boosting with native categorical support — classification",
    "framework": "catboost",
    "license": "Apache 2.0",
    "uri": "oci://registry.inaiera.io/mlops/trainer-catboost:1.2",
    "gpu": true,
    "computeProfile": "cpu-large",
    "longDescription": "Handles categorical features natively — no manual one-hot or target encoding needed. The pick when the dataset is heavy on categorical columns.",
    "category": "classification",
    "algorithmKey": "catboost",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-classifier",
      "lightgbm-ranker"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "linear-svm",
    "domain": "ml",
    "title": "linear-svm",
    "task": "Classification",
    "description": "Linear support vector classifier — large/sparse data",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.svm.LinearSVC",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "A linear-only SVM — much faster than kernel SVM on large datasets or high-dimensional sparse features like text, at the cost of only linear decision boundaries.",
    "category": "classification",
    "algorithmKey": "linear_svm",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "svm"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "adaboost-classifier",
    "domain": "ml",
    "title": "adaboost-classifier",
    "task": "Classification",
    "description": "Adaptive boosting — classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.ensemble.AdaBoostClassifier",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Combines many weak learners (usually shallow trees), reweighting misclassified examples each round. Simple, interpretable, sensitive to noisy labels/outliers.",
    "category": "classification",
    "algorithmKey": "adaboost",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-classifier"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "sgd-classifier",
    "domain": "ml",
    "title": "sgd-classifier",
    "task": "Classification",
    "description": "Stochastic gradient descent — classification, very large datasets",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.linear_model.SGDClassifier",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Fits via stochastic gradient descent — scales to datasets too large to hold in memory, since it never needs the full dataset at once.",
    "category": "classification",
    "algorithmKey": "sgd_classifier",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "logistic-regression",
      "linear-svm"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "mlp-classifier",
    "domain": "ml",
    "title": "mlp-classifier",
    "task": "Classification",
    "description": "Small feed-forward neural network — classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.neural_network.MLPClassifier",
    "gpu": false,
    "computeProfile": "cpu-large",
    "longDescription": "A small fully-connected neural network — a reasonable middle ground between classical ML and reaching for a full deep learning framework.",
    "category": "classification",
    "algorithmKey": "mlp",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-classifier"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "lda-classifier",
    "domain": "ml",
    "title": "lda-classifier",
    "task": "Classification",
    "description": "Linear Discriminant Analysis — classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.discriminant_analysis.LinearDiscriminantAnalysis",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Finds the linear combination of features that best separates classes — fast, interpretable, and doubles as a dimensionality reduction step.",
    "category": "classification",
    "algorithmKey": "lda",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "qda-classifier"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "qda-classifier",
    "domain": "ml",
    "title": "qda-classifier",
    "task": "Classification",
    "description": "Quadratic Discriminant Analysis — classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.discriminant_analysis.QuadraticDiscriminantAnalysis",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Like LDA, but allows each class its own covariance structure — a more flexible boundary at the cost of needing more data per class to estimate it well.",
    "category": "classification",
    "algorithmKey": "qda",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "lda-classifier"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "gaussian-naive-bayes",
    "domain": "ml",
    "title": "gaussian-naive-bayes",
    "task": "Classification",
    "description": "Naive Bayes for continuous features — classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.naive_bayes.GaussianNB",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Assumes continuous features are normally distributed within each class — the right Naive Bayes variant for numeric, non-count features.",
    "category": "classification",
    "algorithmKey": "naive_bayes",
    "algorithmVariant": "gaussian",
    "params": null,
    "context": null,
    "alternatives": [
      "multinomial-naive-bayes",
      "bernoulli-naive-bayes"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "multinomial-naive-bayes",
    "domain": "ml",
    "title": "multinomial-naive-bayes",
    "task": "Classification",
    "description": "Naive Bayes for count/TF-IDF features — text classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.naive_bayes.MultinomialNB",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "The standard Naive Bayes for text classification — built for word-count or TF-IDF style features, not raw continuous values.",
    "category": "classification",
    "algorithmKey": "naive_bayes",
    "algorithmVariant": "multinomial",
    "params": null,
    "context": null,
    "alternatives": [
      "gaussian-naive-bayes",
      "bernoulli-naive-bayes"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "bernoulli-naive-bayes",
    "domain": "ml",
    "title": "bernoulli-naive-bayes",
    "task": "Classification",
    "description": "Naive Bayes for binary/boolean features — classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.naive_bayes.BernoulliNB",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Built for binary/boolean features (word present or not, rather than word count) — often used for short-text classification.",
    "category": "classification",
    "algorithmKey": "naive_bayes",
    "algorithmVariant": "bernoulli",
    "params": null,
    "context": null,
    "alternatives": [
      "multinomial-naive-bayes",
      "gaussian-naive-bayes"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "decision-tree-classifier",
    "domain": "ml",
    "title": "decision-tree-classifier",
    "task": "Classification",
    "description": "Single interpretable decision tree — classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.tree.DecisionTreeClassifier",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "A single tree — the most interpretable model in the library, useful when you need to explain an individual prediction to a non-technical stakeholder.",
    "category": "classification",
    "algorithmKey": "decision_tree",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "random-forest",
      "extra-trees-classifier"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "knn-classifier",
    "domain": "ml",
    "title": "knn-classifier",
    "task": "Classification",
    "description": "K-nearest neighbors — classification",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.neighbors.KNeighborsClassifier",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Classifies by majority vote among the nearest training points — no training phase to speak of, but prediction cost grows with dataset size. Requires feature scaling.",
    "category": "classification",
    "algorithmKey": "knn",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "logistic-regression"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "logistic-regression",
    "domain": "ml",
    "title": "logistic-regression",
    "task": "Classification",
    "description": "Binary/multi-class classification, external (Ray Train)",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.linear_model.LogisticRegression",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "The standard linear baseline for classification — fast, interpretable coefficients, a good first model before reaching for anything more complex.",
    "category": "classification",
    "algorithmKey": "logistic_regression",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "linear-svm"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "svm",
    "domain": "ml",
    "title": "svm",
    "task": "Classification / regression",
    "description": "Support vector machine, external (Ray Train)",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.svm.SVC",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Kernel support vector machine — strong on smaller-to-medium datasets with a clear margin between classes. Requires feature scaling.",
    "category": "classification",
    "algorithmKey": "svm",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "linear-svm"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "linear-regression",
    "domain": "ml",
    "title": "linear-regression",
    "task": "Regression",
    "description": "Ordinary least squares — regression, external (Ray Train)",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.linear_model.LinearRegression",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Plain ordinary least squares — no regularization. The simplest baseline; reach for Ridge/Lasso/Elastic Net once overfitting shows up.",
    "category": "regression",
    "algorithmKey": "linear_family",
    "algorithmVariant": "ols",
    "params": null,
    "context": null,
    "alternatives": [
      "ridge-regression"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "ridge-regression",
    "domain": "ml",
    "title": "ridge-regression",
    "task": "Regression",
    "description": "L2-regularized linear regression, external (Ray Train)",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.linear_model.Ridge",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Linear regression with L2 regularization — shrinks coefficients toward zero without eliminating any, good when most features genuinely matter a little.",
    "category": "regression",
    "algorithmKey": "linear_family",
    "algorithmVariant": "ridge",
    "params": null,
    "context": null,
    "alternatives": [
      "lasso-regression",
      "elasticnet-regression"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "lasso-regression",
    "domain": "ml",
    "title": "lasso-regression",
    "task": "Regression",
    "description": "L1-regularized linear regression — feature selection",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.linear_model.Lasso",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Linear regression with L1 regularization — can shrink coefficients all the way to zero, effectively performing feature selection during training.",
    "category": "regression",
    "algorithmKey": "linear_family",
    "algorithmVariant": "lasso",
    "params": null,
    "context": null,
    "alternatives": [
      "ridge-regression",
      "elasticnet-regression"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "elasticnet-regression",
    "domain": "ml",
    "title": "elasticnet-regression",
    "task": "Regression",
    "description": "L1+L2-regularized linear regression, external (Ray Train)",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.linear_model.ElasticNet",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Mixes L1 and L2 regularization — feature selection like Lasso, stability like Ridge, tunable via the L1 ratio.",
    "category": "regression",
    "algorithmKey": "linear_family",
    "algorithmVariant": "elasticnet",
    "params": null,
    "context": null,
    "alternatives": [
      "ridge-regression",
      "lasso-regression"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "decision-tree-regressor",
    "domain": "ml",
    "title": "decision-tree-regressor",
    "task": "Regression",
    "description": "Single interpretable decision tree — regression",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.tree.DecisionTreeRegressor",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "A single tree for regression — highly interpretable, but prone to overfitting on its own; usually a component of an ensemble rather than the final model.",
    "category": "regression",
    "algorithmKey": "decision_tree",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "random-forest"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "xgboost-regressor",
    "domain": "ml",
    "title": "xgboost-regressor",
    "task": "Regression",
    "description": "Gradient boosting — regression",
    "framework": "xgboost",
    "license": "Apache 2.0",
    "uri": "oci://registry.inaiera.io/mlops/trainer-xgboost:1.4",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "The same XGBoost algorithm as xgboost-classifier, listed separately here so it shows up under the Regression category filter too.",
    "category": "regression",
    "algorithmKey": "xgboost",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-classifier",
      "lightgbm-regressor"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "random-forest-regressor",
    "domain": "ml",
    "title": "random-forest-regressor",
    "task": "Regression",
    "description": "Ensemble trees — regression",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.ensemble.RandomForestRegressor",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "The same Random Forest algorithm as random-forest, listed separately here so it shows up under the Regression category filter too.",
    "category": "regression",
    "algorithmKey": "random_forest",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "random-forest",
      "extra-trees-regressor"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "extra-trees-regressor",
    "domain": "ml",
    "title": "extra-trees-regressor",
    "task": "Regression",
    "description": "Randomized ensemble trees — regression",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.ensemble.ExtraTreesRegressor",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Random-split ensemble trees for regression — usually faster to train than Random Forest with comparable accuracy.",
    "category": "regression",
    "algorithmKey": "extra_trees",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "random-forest"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "gradient-boosting-regressor",
    "domain": "ml",
    "title": "gradient-boosting-regressor",
    "task": "Regression",
    "description": "sklearn's native gradient boosting — regression",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.ensemble.GradientBoostingRegressor",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "sklearn's own boosting implementation for regression, including quantile and Huber loss for robust/interval forecasting.",
    "category": "regression",
    "algorithmKey": "gradient_boosting",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-classifier",
      "lightgbm-regressor"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "lightgbm-regressor",
    "domain": "ml",
    "title": "lightgbm-regressor",
    "task": "Regression",
    "description": "Fast gradient boosting — regression",
    "framework": "lightgbm",
    "license": "MIT",
    "uri": "oci://registry.inaiera.io/mlops/trainer-lightgbm:1.2",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "The regression-flavored counterpart to lightgbm-ranker — same histogram-based boosting, tuned for continuous targets on large tabular data.",
    "category": "regression",
    "algorithmKey": "lightgbm",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-classifier",
      "catboost-regressor"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "catboost-regressor",
    "domain": "ml",
    "title": "catboost-regressor",
    "task": "Regression",
    "description": "Gradient boosting with native categorical support — regression",
    "framework": "catboost",
    "license": "Apache 2.0",
    "uri": "oci://registry.inaiera.io/mlops/trainer-catboost:1.2",
    "gpu": true,
    "computeProfile": "cpu-large",
    "longDescription": "CatBoost's regression mode — same native categorical-feature handling as the classifier, for continuous targets.",
    "category": "regression",
    "algorithmKey": "catboost",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-classifier",
      "lightgbm-regressor"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "svr",
    "domain": "ml",
    "title": "svr",
    "task": "Regression",
    "description": "Support vector regression, external (Ray Train)",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.svm.SVR",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "The regression counterpart to SVM/SVC — fits within an epsilon-wide margin rather than a hard classification boundary. Requires feature scaling.",
    "category": "regression",
    "algorithmKey": "svm",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "svm"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "knn-regressor",
    "domain": "ml",
    "title": "knn-regressor",
    "task": "Regression",
    "description": "K-nearest neighbors — regression",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.neighbors.KNeighborsRegressor",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Predicts by averaging the nearest training points' targets — a fast, non-parametric baseline. Requires feature scaling.",
    "category": "regression",
    "algorithmKey": "knn",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "knn-classifier"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "adaboost-regressor",
    "domain": "ml",
    "title": "adaboost-regressor",
    "task": "Regression",
    "description": "Adaptive boosting — regression",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.ensemble.AdaBoostRegressor",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "AdaBoost's regression mode — combines weak learners with reweighting, same simplicity/noise-sensitivity trade-off as the classifier.",
    "category": "regression",
    "algorithmKey": "adaboost",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "gradient-boosting-regressor"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "huber-regressor",
    "domain": "ml",
    "title": "huber-regressor",
    "task": "Regression",
    "description": "Outlier-robust linear regression",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.linear_model.HuberRegressor",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Linear regression that's far less sensitive to outliers than OLS — behaves quadratically near the bulk of the data, linearly beyond the epsilon threshold.",
    "category": "regression",
    "algorithmKey": "huber_regressor",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "ridge-regression"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "bayesian-ridge",
    "domain": "ml",
    "title": "bayesian-ridge",
    "task": "Regression",
    "description": "Bayesian linear regression with uncertainty estimates",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.linear_model.BayesianRidge",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Regularization strength is learned automatically rather than tuned by hand, and predictions come with an uncertainty estimate, not just a point value.",
    "category": "regression",
    "algorithmKey": "bayesian_ridge",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "ridge-regression"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "poisson-regressor",
    "domain": "ml",
    "title": "poisson-regressor",
    "task": "Regression",
    "description": "Regression for count-valued targets",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.linear_model.PoissonRegressor",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "For targets that are non-negative counts (number of events, claims, visits) — models a Poisson distribution instead of assuming Gaussian errors.",
    "category": "regression",
    "algorithmKey": "poisson_regressor",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "tweedie-regressor"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "tweedie-regressor",
    "domain": "ml",
    "title": "tweedie-regressor",
    "task": "Regression",
    "description": "Regression for skewed, non-negative targets",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.linear_model.TweedieRegressor",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Generalizes Poisson/Gamma/Gaussian regression under one power parameter — common in insurance claims and rainfall modeling.",
    "category": "regression",
    "algorithmKey": "tweedie_regressor",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "poisson-regressor"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "mlp-regressor",
    "domain": "ml",
    "title": "mlp-regressor",
    "task": "Regression",
    "description": "Small feed-forward neural network — regression",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.neural_network.MLPRegressor",
    "gpu": false,
    "computeProfile": "cpu-large",
    "longDescription": "The regression counterpart to mlp-classifier — a small fully-connected network, a middle ground before reaching for a full deep learning framework.",
    "category": "regression",
    "algorithmKey": "mlp",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "gradient-boosting-regressor"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "kmeans-clustering",
    "domain": "ml",
    "title": "kmeans-clustering",
    "task": "Clustering",
    "description": "Centroid-based clustering — general purpose",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.cluster.KMeans",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "The default clustering choice when you know roughly how many groups to expect and they're reasonably compact/spherical.",
    "category": "clustering",
    "algorithmKey": "kmeans",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "gaussian-mixture",
      "agglomerative-clustering"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "dbscan-clustering",
    "domain": "ml",
    "title": "dbscan-clustering",
    "task": "Clustering",
    "description": "Density-based clustering — arbitrary shapes, auto-detects noise",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.cluster.DBSCAN",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Finds clusters of arbitrary shape and labels sparse points as noise automatically — no need to pre-specify how many clusters exist.",
    "category": "clustering",
    "algorithmKey": "dbscan",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "kmeans-clustering",
      "spectral-clustering"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "agglomerative-clustering",
    "domain": "ml",
    "title": "agglomerative-clustering",
    "task": "Clustering",
    "description": "Hierarchical clustering using a bottom-up approach",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.cluster.AgglomerativeClustering",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Hierarchical clustering using a bottom-up approach — builds a dendrogram, useful when you want to inspect the merge structure, not just a flat partition.",
    "category": "clustering",
    "algorithmKey": "agglomerative_clustering",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "kmeans-clustering",
      "birch-clustering"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "gaussian-mixture",
    "domain": "ml",
    "title": "gaussian-mixture",
    "task": "Clustering",
    "description": "Soft, probabilistic clustering with elliptical clusters",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.mixture.GaussianMixture",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Gives each point a probability of belonging to each cluster rather than a hard assignment, and allows elliptical (not just spherical) cluster shapes.",
    "category": "clustering",
    "algorithmKey": "gaussian_mixture",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "kmeans-clustering"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "mean-shift-clustering",
    "domain": "ml",
    "title": "mean-shift-clustering",
    "task": "Clustering",
    "description": "Density-peak clustering — auto-detects cluster count",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.cluster.MeanShift",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Discovers the number of clusters automatically by locating density peaks — useful when you genuinely don't know how many groups to expect.",
    "category": "clustering",
    "algorithmKey": "mean_shift",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "dbscan-clustering"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "spectral-clustering",
    "domain": "ml",
    "title": "spectral-clustering",
    "task": "Clustering",
    "description": "Graph-based clustering — non-convex cluster shapes",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.cluster.SpectralClustering",
    "gpu": false,
    "computeProfile": "cpu-large",
    "longDescription": "Embeds points using graph structure before clustering — handles non-convex shapes KMeans can't, at higher computational cost.",
    "category": "clustering",
    "algorithmKey": "spectral_clustering",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "dbscan-clustering"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "birch-clustering",
    "domain": "ml",
    "title": "birch-clustering",
    "task": "Clustering",
    "description": "Clustering for very large datasets",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.cluster.Birch",
    "gpu": false,
    "computeProfile": "cpu-large",
    "longDescription": "Builds a compact tree summary of the data first, then clusters that — scales to datasets too large to cluster directly in memory.",
    "category": "clustering",
    "algorithmKey": "birch",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "kmeans-clustering"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "isolation-forest",
    "domain": "ml",
    "title": "isolation-forest",
    "task": "Anomaly detection",
    "description": "General anomaly detection — no distribution assumptions",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.ensemble.IsolationForest",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Isolates outliers with fewer random splits than normal points need — the default general-purpose anomaly detector, no assumption about data distribution.",
    "category": "anomaly_detection",
    "algorithmKey": "isolation_forest",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "local-outlier-factor",
      "one-class-svm"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "local-outlier-factor",
    "domain": "ml",
    "title": "local-outlier-factor",
    "task": "Anomaly detection",
    "description": "Local anomalies — clusters of varying density",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.neighbors.LocalOutlierFactor",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Flags points that are unusual relative to their local neighborhood — better than Isolation Forest when normal data has clusters of very different densities.",
    "category": "anomaly_detection",
    "algorithmKey": "local_outlier_factor",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "isolation-forest"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "one-class-svm",
    "domain": "ml",
    "title": "one-class-svm",
    "task": "Anomaly detection",
    "description": "Anomaly detection with complex decision boundaries",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.svm.OneClassSVM",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Fits a boundary around the 'normal' data using the SVM kernel trick — handles complex boundaries Isolation Forest's axis-aligned splits can miss.",
    "category": "anomaly_detection",
    "algorithmKey": "one_class_svm",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "isolation-forest"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "elliptic-envelope",
    "domain": "ml",
    "title": "elliptic-envelope",
    "task": "Anomaly detection",
    "description": "Anomaly detection for Gaussian-distributed data",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.covariance.EllipticEnvelope",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Fits an ellipse around the bulk of the data — the right tool specifically when normal data is roughly Gaussian; a poor fit otherwise.",
    "category": "anomaly_detection",
    "algorithmKey": "elliptic_envelope",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "isolation-forest"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "dbscan-anomaly-detection",
    "domain": "ml",
    "title": "dbscan-anomaly-detection",
    "task": "Anomaly detection",
    "description": "Density-based anomaly detection",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.cluster.DBSCAN",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Points DBSCAN can't assign to any dense region are natural anomaly candidates — reuses the same clustering algorithm for a detection task.",
    "category": "anomaly_detection",
    "algorithmKey": "dbscan",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "isolation-forest"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "autoencoder-anomaly",
    "domain": "ml",
    "title": "autoencoder-anomaly",
    "task": "Anomaly detection",
    "description": "Deep anomaly detection — complex, high-dimensional data",
    "framework": "pytorch",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-autoencoder:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "Flags points the network reconstructs poorly as anomalies — the right tool when the data is too high-dimensional or complex for tree/distance-based methods.",
    "category": "anomaly_detection",
    "algorithmKey": "autoencoder",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "isolation-forest"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "lstm-autoencoder",
    "domain": "ml",
    "title": "lstm-autoencoder",
    "task": "Anomaly detection",
    "description": "Deep anomaly detection for time-series",
    "framework": "pytorch",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-lstm-ae:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "Learns to reconstruct normal sequences — sequences with high reconstruction error are flagged as anomalous. For time-series specifically, not tabular data.",
    "category": "anomaly_detection",
    "algorithmKey": "lstm_autoencoder",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "autoencoder-anomaly"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "arima-forecaster",
    "domain": "ml",
    "title": "arima-forecaster",
    "task": "Forecasting",
    "description": "Classic Box-Jenkins forecasting — trend, no seasonality",
    "framework": "statsmodels",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-statsmodels-arima:1.0",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "The classic Box-Jenkins model — good for univariate series with trend but no strong seasonality.",
    "category": "timeseries",
    "algorithmKey": "arima",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "sarima-forecaster",
      "auto-arima-forecaster"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "sarima-forecaster",
    "domain": "ml",
    "title": "sarima-forecaster",
    "task": "Forecasting",
    "description": "ARIMA with seasonality",
    "framework": "statsmodels",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-statsmodels-sarima:1.0",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "ARIMA extended with seasonal terms — the right choice once a series has a clear recurring cycle (weekly, yearly) on top of its trend.",
    "category": "timeseries",
    "algorithmKey": "sarima",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "arima-forecaster",
      "auto-arima-forecaster"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "auto-arima-forecaster",
    "domain": "ml",
    "title": "auto-arima-forecaster",
    "task": "Forecasting",
    "description": "ARIMA/SARIMA with automatic order search",
    "framework": "pmdarima",
    "license": "MIT",
    "uri": "oci://registry.inaiera.io/mlops/trainer-pmdarima:1.0",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Same model family as SARIMA, but searches for the best orders automatically instead of you specifying (p,d,q)(P,D,Q) by hand.",
    "category": "timeseries",
    "algorithmKey": "auto_arima",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "sarima-forecaster"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "exponential-smoothing",
    "domain": "ml",
    "title": "exponential-smoothing",
    "task": "Forecasting",
    "description": "Fast, simple forecasting baseline",
    "framework": "statsmodels",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-statsmodels-ets:1.0",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Simple, fast, with optional trend and seasonality — a strong baseline to beat before reaching for ARIMA or ML-based forecasters.",
    "category": "timeseries",
    "algorithmKey": "exponential_smoothing",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "holt-winters"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "holt-winters",
    "domain": "ml",
    "title": "holt-winters",
    "task": "Forecasting",
    "description": "Triple exponential smoothing — level, trend, seasonality",
    "framework": "statsmodels",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-statsmodels-holtwinters:1.0",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "The most complete member of the exponential smoothing family — level, trend, and seasonality modeled together.",
    "category": "timeseries",
    "algorithmKey": "holt_winters",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "exponential-smoothing"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "xgboost-forecaster",
    "domain": "ml",
    "title": "xgboost-forecaster",
    "task": "Forecasting",
    "description": "Gradient boosting applied to windowed time-series features",
    "framework": "xgboost",
    "license": "Apache 2.0",
    "uri": "oci://registry.inaiera.io/mlops/trainer-xgboost:1.4",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "The same XGBoost algorithm as xgboost-classifier, applied to lag/rolling-window features engineered from a time series — often competitive with dedicated forecasters.",
    "category": "timeseries",
    "algorithmKey": "xgboost",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "lightgbm-forecaster",
      "lstm-forecaster"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "lightgbm-forecaster",
    "domain": "ml",
    "title": "lightgbm-forecaster",
    "task": "Forecasting",
    "description": "Fast gradient boosting applied to windowed time-series features",
    "framework": "lightgbm",
    "license": "MIT",
    "uri": "oci://registry.inaiera.io/mlops/trainer-lightgbm:1.2",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "LightGBM on lag/rolling-window features — fast to train, a strong tabular-ML alternative to classical statistical forecasters.",
    "category": "timeseries",
    "algorithmKey": "lightgbm",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "xgboost-forecaster"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "lstm-forecaster",
    "domain": "ml",
    "title": "lstm-forecaster",
    "task": "Forecasting",
    "description": "Deep forecasting for long-range dependencies",
    "framework": "pytorch",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-lstm:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "A recurrent network trained on windowed sequences — worth the extra compute when a series has long-range dependencies simpler models miss.",
    "category": "timeseries",
    "algorithmKey": "lstm_forecaster",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "gru-forecaster",
      "xgboost-forecaster"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "gru-forecaster",
    "domain": "ml",
    "title": "gru-forecaster",
    "task": "Forecasting",
    "description": "Lighter-weight recurrent forecasting",
    "framework": "pytorch",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-gru:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "Similar accuracy to LSTM forecasting with fewer parameters — a lighter-weight recurrent option worth trying first.",
    "category": "timeseries",
    "algorithmKey": "gru_forecaster",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "lstm-forecaster"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "temporal-fusion-transformer",
    "domain": "ml",
    "title": "temporal-fusion-transformer",
    "task": "Forecasting",
    "description": "Attention-based forecasting with interpretable importance",
    "framework": "pytorch",
    "license": "Apache 2.0",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-tft:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "Handles many known future inputs (prices, promotions) and surfaces which timesteps and features mattered most via attention — more interpretable than most deep forecasters.",
    "category": "timeseries",
    "algorithmKey": "tft",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "nbeats-forecaster",
      "lstm-forecaster"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "nbeats-forecaster",
    "domain": "ml",
    "title": "nbeats-forecaster",
    "task": "Forecasting",
    "description": "Pure deep-learning forecasting, no hand-crafted features",
    "framework": "pytorch",
    "license": "Apache 2.0",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-nbeats:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "A stack of fully-connected blocks that learns its own basis functions — no seasonal/trend features to hand-engineer, strong on M-competition style benchmarks.",
    "category": "timeseries",
    "algorithmKey": "nbeats",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "temporal-fusion-transformer"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "pca-reduction",
    "domain": "ml",
    "title": "pca-reduction",
    "task": "Dimensionality reduction",
    "description": "Linear dimensionality reduction — variance-maximizing projection",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.decomposition.PCA",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Projects onto the directions of maximum variance — the default first thing to try for reduction, denoising, or visualization prep.",
    "category": "dimensionality_reduction",
    "algorithmKey": "pca",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "kernel-pca",
      "truncated-svd"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "incremental-pca",
    "domain": "ml",
    "title": "incremental-pca",
    "task": "Dimensionality reduction",
    "description": "PCA for datasets too large to fit in memory",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.decomposition.IncrementalPCA",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Same idea as PCA, processed in mini-batches — use this instead of plain PCA when the dataset doesn't fit in memory at once.",
    "category": "dimensionality_reduction",
    "algorithmKey": "incremental_pca",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "pca-reduction"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "kernel-pca",
    "domain": "ml",
    "title": "kernel-pca",
    "task": "Dimensionality reduction",
    "description": "Non-linear dimensionality reduction via the kernel trick",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.decomposition.KernelPCA",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Applies the kernel trick so PCA can capture curved, non-linear structure that plain PCA's linear projection misses.",
    "category": "dimensionality_reduction",
    "algorithmKey": "kernel_pca",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "pca-reduction"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "truncated-svd",
    "domain": "ml",
    "title": "truncated-svd",
    "task": "Dimensionality reduction",
    "description": "PCA-like reduction for sparse data (e.g. TF-IDF)",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.decomposition.TruncatedSVD",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "PCA without centering, so it works directly on sparse matrices like TF-IDF text features without densifying them first.",
    "category": "dimensionality_reduction",
    "algorithmKey": "truncated_svd",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "pca-reduction"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "fastica",
    "domain": "ml",
    "title": "fastica",
    "task": "Dimensionality reduction",
    "description": "Independent component analysis — signal separation",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.decomposition.FastICA",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Separates a multivariate signal into statistically independent components — for source/signal separation, not just variance-based reduction.",
    "category": "dimensionality_reduction",
    "algorithmKey": "fastica",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "pca-reduction"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "tsne",
    "domain": "ml",
    "title": "tsne",
    "task": "Dimensionality reduction",
    "description": "2D/3D visualization of high-dimensional data",
    "framework": "sklearn",
    "license": "BSD-3-Clause",
    "uri": "sklearn://sklearn.manifold.TSNE",
    "gpu": false,
    "computeProfile": "cpu-large",
    "longDescription": "Preserves local neighborhood structure for visualization — built for plotting, not as a general-purpose feature-reduction step for downstream models.",
    "category": "dimensionality_reduction",
    "algorithmKey": "tsne",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "umap-reduction"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "umap-reduction",
    "domain": "ml",
    "title": "umap-reduction",
    "task": "Dimensionality reduction",
    "description": "Faster, more scalable alternative to t-SNE",
    "framework": "umap-learn",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-umap:0.5",
    "gpu": false,
    "computeProfile": "cpu-small",
    "longDescription": "Faster and more scalable than t-SNE, and better preserves global structure — usable for both visualization and general-purpose reduction.",
    "category": "dimensionality_reduction",
    "algorithmKey": "umap",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "tsne",
      "pca-reduction"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "deep-mlp-classifier",
    "domain": "ml",
    "title": "deep-mlp-classifier",
    "task": "Classification",
    "description": "Deep feedforward network — classification",
    "framework": "pytorch",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-mlp-classifier:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "A multi-layer feedforward network deeper than sklearn's mlp-classifier — several hidden layers trained with backprop on GPU. Worth it once a shallow MLP underfits; overkill for most tabular problems xgboost already handles well.",
    "category": "classification",
    "algorithmKey": "deep_mlp",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "mlp-classifier",
      "xgboost-classifier"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "deep-mlp-regressor",
    "domain": "ml",
    "title": "deep-mlp-regressor",
    "task": "Regression",
    "description": "Deep feedforward network — regression",
    "framework": "pytorch",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-mlp-regressor:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "The regression counterpart to deep-mlp-classifier — a deep feedforward network for continuous targets. Same tradeoff: only reach for this over gradient boosting when there's a genuine nonlinear-interaction signal a tree model is missing.",
    "category": "regression",
    "algorithmKey": "deep_mlp",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "mlp-regressor",
      "xgboost-regressor"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "cnn-image-classifier",
    "domain": "ml",
    "title": "cnn-image-classifier",
    "task": "Image classification",
    "description": "Convolutional network — image classification",
    "framework": "pytorch",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-cnn:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "A convolutional network for image inputs — the only base model in the library built for pixel data rather than tabular or sequence features. Use this when the input is genuinely an image (e.g. damage photos, document scans), not for tabular data reshaped to look image-like.",
    "category": "classification",
    "algorithmKey": "cnn",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "gru-classifier",
    "domain": "ml",
    "title": "gru-classifier",
    "task": "Classification",
    "description": "Recurrent network — sequence classification",
    "framework": "pytorch",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-gru-classifier:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "GRU encoder for classifying a sequence as a whole (e.g. a session of events, a sensor window) rather than forecasting its next value — pairs with gru-forecaster and lstm-forecaster, which handle the regression/forecasting side of recurrent modeling.",
    "category": "classification",
    "algorithmKey": "gru_classifier",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "lstm-forecaster",
      "gru-forecaster"
    ],
    "defaultHp": null,
    "supportedHp": null
  },
  {
    "id": "denoising-autoencoder",
    "domain": "ml",
    "title": "denoising-autoencoder",
    "task": "Dimensionality reduction",
    "description": "Deep autoencoder — representation learning",
    "framework": "pytorch",
    "license": "BSD-3-Clause",
    "uri": "oci://registry.inaiera.io/mlops/trainer-torch-denoising-ae:1.0",
    "gpu": true,
    "computeProfile": "gpu-1x",
    "longDescription": "Learns a compressed, denoised representation of the input — for dimensionality reduction and feature learning ahead of a downstream model, not anomaly scoring directly (see autoencoder-anomaly and lstm-autoencoder for that).",
    "category": "dimensionality_reduction",
    "algorithmKey": "autoencoder",
    "algorithmVariant": null,
    "params": null,
    "context": null,
    "alternatives": [
      "autoencoder-anomaly",
      "lstm-autoencoder"
    ],
    "defaultHp": null,
    "supportedHp": null
  }
];
