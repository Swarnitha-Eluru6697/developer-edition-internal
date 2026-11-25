/**
 * The hyperparameter surface of every trainer this platform can dispatch to —
 * 56 algorithms, keyed by the `algorithm_key` each base_models row carries.
 *
 * This is what the Train wizard renders in fixed-hyperparameter mode and what
 * POST /api/training-runs validates a submitted `hyperparams` object against,
 * so a typo or an out-of-range value fails at submit rather than inside a
 * Python worker minutes later. Each field declares its real type, default,
 * documented range, and what it does.
 *
 * `hasAppliesTo` marks a family whose members are genuinely different
 * estimators sharing one trainer (the linear family: OLS / ridge / lasso /
 * elastic-net). A field with `showIf` exists only for those variants —
 * `l1_ratio` is meaningless for plain ridge — and the variant itself comes
 * from the base model's `algorithm_variant`, not from a separate picker.
 */
export const ALGORITHM_SPECS = [
  {
    "algorithmKey": "linear_family",
    "label": "Linear Regression",
    "appliesToText": "regression, and numeric forecasting-style tasks with tabular features.",
    "hasAppliesTo": true,
    "defaultAppliesTo": "elasticnet",
    "appliesToTooltip": "Which regularization variant of the linear model to train.",
    "appliesToOptions": [
      {
        "value": "ols",
        "label": "Ordinary Least Squares"
      },
      {
        "value": "ridge",
        "label": "Ridge (L2)"
      },
      {
        "value": "lasso",
        "label": "Lasso (L1)"
      },
      {
        "value": "elasticnet",
        "label": "Elastic Net (L1 + L2)"
      }
    ],
    "fields": [
      {
        "name": "alpha",
        "label": "Alpha (Regularization Strength)",
        "type": "number",
        "default": 1,
        "range": "0.0001 – 100",
        "required": true,
        "tooltip": "Regularization strength. Higher values reduce overfitting.",
        "showIf": [
          "ridge",
          "lasso",
          "elasticnet"
        ]
      },
      {
        "name": "l1_ratio",
        "label": "L1 Ratio (Mix Parameter)",
        "type": "number",
        "default": 0.5,
        "range": "0 – 1",
        "required": true,
        "tooltip": "Mix between L1 and L2 penalty. 0 = Ridge-like, 1 = Lasso-like.",
        "showIf": [
          "elasticnet"
        ]
      },
      {
        "name": "fit_intercept",
        "label": "Fit Intercept",
        "type": "yesno",
        "default": "yes",
        "tooltip": "Whether to fit intercept/bias term."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 5000,
        "range": "1000 – 10000",
        "tooltip": "Maximum optimization iterations.",
        "showIf": [
          "lasso",
          "elasticnet"
        ]
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.001",
        "tooltip": "Optimization tolerance. Lower is stricter.",
        "showIf": [
          "lasso",
          "elasticnet"
        ]
      },
      {
        "name": "positive",
        "label": "Force Positive Coefficients",
        "type": "yesno",
        "default": "no",
        "tooltip": "Forces positive coefficients where supported."
      }
    ]
  },
  {
    "algorithmKey": "logistic_regression",
    "label": "Logistic Regression",
    "appliesToText": "binary classification and multi-class classification.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "C",
        "label": "C (Inverse Regularization)",
        "type": "number",
        "default": 1,
        "range": "0.01 – 100",
        "required": true,
        "tooltip": "Inverse regularization strength. Smaller values mean stronger regularization."
      },
      {
        "name": "penalty",
        "label": "Penalty",
        "type": "select",
        "default": "l2",
        "options": [
          "l1",
          "l2",
          "elasticnet",
          "none"
        ],
        "tooltip": "Regularization type."
      },
      {
        "name": "solver",
        "label": "Solver",
        "type": "select",
        "default": "lbfgs",
        "options": [
          "lbfgs",
          "liblinear",
          "saga",
          "sag",
          "newton-cg"
        ],
        "tooltip": "Optimization algorithm. Must be compatible with penalty."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 1000,
        "range": "100 – 5000",
        "tooltip": "Maximum optimizer iterations. Increase if convergence warnings occur."
      },
      {
        "name": "class_weight",
        "label": "Class Weight",
        "type": "select",
        "default": "none",
        "options": [
          "none",
          "balanced"
        ],
        "tooltip": "Handles imbalanced classes."
      },
      {
        "name": "l1_ratio",
        "label": "L1 Ratio",
        "type": "number",
        "default": 0.5,
        "range": "0 – 1",
        "tooltip": "Elastic-net mixing ratio. Only applies when penalty = elasticnet."
      },
      {
        "name": "multi_class",
        "label": "Multi-class Mode",
        "type": "select",
        "default": "auto",
        "options": [
          "auto",
          "ovr",
          "multinomial"
        ],
        "tooltip": "Multi-class handling mode where supported."
      }
    ]
  },
  {
    "algorithmKey": "xgboost",
    "label": "XGBoost",
    "appliesToText": "binary classification, multi-class classification, regression, and ranking-style tasks.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_estimators",
        "label": "N Estimators",
        "type": "number",
        "default": 800,
        "range": "100 – 2000",
        "required": true,
        "tooltip": "Number of boosting rounds / trees."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate (eta)",
        "type": "number",
        "default": 0.1,
        "range": "0.01 – 0.3",
        "required": true,
        "tooltip": "Step size shrinkage after each boosting round."
      },
      {
        "name": "max_depth",
        "label": "Max Depth",
        "type": "number",
        "default": 6,
        "range": "3 – 10",
        "tooltip": "Maximum depth of each tree."
      },
      {
        "name": "min_child_weight",
        "label": "Min Child Weight",
        "type": "number",
        "default": 1,
        "range": "1 – 10",
        "tooltip": "Minimum sum of instance weight needed in a child."
      },
      {
        "name": "gamma",
        "label": "Gamma",
        "type": "number",
        "default": 0,
        "range": "0 – 10",
        "tooltip": "Minimum loss reduction required to make a split."
      },
      {
        "name": "subsample",
        "label": "Subsample",
        "type": "number",
        "default": 0.8,
        "range": "0.5 – 1.0",
        "tooltip": "Row sampling ratio per tree."
      },
      {
        "name": "colsample_bytree",
        "label": "Colsample by Tree",
        "type": "number",
        "default": 0.8,
        "range": "0.5 – 1.0",
        "tooltip": "Feature sampling ratio per tree."
      },
      {
        "name": "colsample_bylevel",
        "label": "Colsample by Level",
        "type": "number",
        "default": 1,
        "range": "0.5 – 1.0",
        "tooltip": "Feature sampling ratio per tree level."
      },
      {
        "name": "colsample_bynode",
        "label": "Colsample by Node",
        "type": "number",
        "default": 1,
        "range": "0.5 – 1.0",
        "tooltip": "Feature sampling ratio per split/node."
      },
      {
        "name": "reg_lambda",
        "label": "Lambda (L2 Reg)",
        "type": "number",
        "default": 1,
        "range": "0 – 10",
        "tooltip": "L2 regularization on weights."
      },
      {
        "name": "reg_alpha",
        "label": "Alpha (L1 Reg)",
        "type": "number",
        "default": 0,
        "range": "0 – 10",
        "tooltip": "L1 regularization on weights. Useful for sparse feature selection."
      },
      {
        "name": "tree_method",
        "label": "Tree Method",
        "type": "select",
        "default": "auto",
        "options": [
          "auto",
          "hist",
          "gpu_hist"
        ],
        "tooltip": "Tree construction algorithm. GPU setups may use gpu_hist."
      },
      {
        "name": "eval_metric",
        "label": "Eval Metric",
        "type": "select",
        "default": "rmse",
        "options": [
          "rmse",
          "mae",
          "logloss",
          "auc",
          "mlogloss"
        ],
        "tooltip": "Metric used during training/evaluation."
      }
    ]
  },
  {
    "algorithmKey": "random_forest",
    "label": "Random Forest",
    "appliesToText": "binary classification, multi-class classification, and regression.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_estimators",
        "label": "N Estimators",
        "type": "number",
        "default": 500,
        "range": "100 – 1000",
        "required": true,
        "tooltip": "Number of trees. More trees improve stability but increase cost."
      },
      {
        "name": "max_depth",
        "label": "Max Depth (blank = full growth)",
        "type": "number",
        "default": 20,
        "range": "5 – 50, or blank",
        "tooltip": "Maximum tree depth. Blank allows full growth."
      },
      {
        "name": "min_samples_split",
        "label": "Min Samples Split",
        "type": "select",
        "default": "2",
        "options": [
          "2",
          "5",
          "10",
          "20"
        ],
        "tooltip": "Minimum samples needed to split a node."
      },
      {
        "name": "min_samples_leaf",
        "label": "Min Samples Leaf",
        "type": "select",
        "default": "1",
        "options": [
          "1",
          "2",
          "4",
          "10"
        ],
        "tooltip": "Minimum samples required at a leaf."
      },
      {
        "name": "max_features",
        "label": "Max Features",
        "type": "select",
        "default": "sqrt",
        "options": [
          "sqrt",
          "log2",
          "0.5",
          "0.8"
        ],
        "tooltip": "Number/fraction of features considered at each split."
      },
      {
        "name": "bootstrap",
        "label": "Bootstrap",
        "type": "yesno",
        "default": "yes",
        "tooltip": "Whether trees are trained on bootstrap samples."
      },
      {
        "name": "max_samples",
        "label": "Max Samples",
        "type": "number",
        "default": 1,
        "range": "0.5 – 1.0",
        "tooltip": "Fraction of samples used per tree when bootstrap is enabled."
      },
      {
        "name": "criterion",
        "label": "Criterion",
        "type": "select",
        "default": "gini",
        "options": [
          "gini",
          "entropy",
          "log_loss",
          "squared_error",
          "absolute_error"
        ],
        "tooltip": "Split quality measure — gini/entropy/log_loss for classification, squared_error/absolute_error for regression."
      },
      {
        "name": "class_weight",
        "label": "Class Weight",
        "type": "select",
        "default": "none",
        "options": [
          "none",
          "balanced",
          "balanced_subsample"
        ],
        "tooltip": "Handles imbalanced classification."
      }
    ]
  },
  {
    "algorithmKey": "svm",
    "label": "Support Vector Machine",
    "appliesToText": "binary/multi-class classification and regression (SVR) — best for smaller-to-medium datasets, requires feature scaling.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "C",
        "label": "C (Regularization)",
        "type": "number",
        "default": 1,
        "range": "0.01 – 100",
        "required": true,
        "tooltip": "Regularization strength. Higher values fit training data more strictly."
      },
      {
        "name": "kernel",
        "label": "Kernel",
        "type": "select",
        "default": "rbf",
        "options": [
          "linear",
          "rbf",
          "poly",
          "sigmoid"
        ],
        "tooltip": "Kernel type."
      },
      {
        "name": "gamma",
        "label": "Gamma",
        "type": "select",
        "default": "scale",
        "options": [
          "scale",
          "auto",
          "0.001",
          "0.01",
          "0.1",
          "1"
        ],
        "tooltip": "Kernel coefficient for rbf/poly/sigmoid. Controls influence radius."
      },
      {
        "name": "degree",
        "label": "Degree (poly kernel only)",
        "type": "number",
        "default": 3,
        "range": "2 – 5",
        "tooltip": "Polynomial degree. Only applies to the poly kernel."
      },
      {
        "name": "coef0",
        "label": "Coef0 (poly/sigmoid only)",
        "type": "number",
        "default": 0,
        "range": "0 – 1",
        "tooltip": "Independent term for poly and sigmoid kernels."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.001,
        "range": "0.00001 – 0.001",
        "tooltip": "Stopping tolerance."
      },
      {
        "name": "class_weight",
        "label": "Class Weight",
        "type": "select",
        "default": "none",
        "options": [
          "none",
          "balanced"
        ],
        "tooltip": "Handles class imbalance."
      },
      {
        "name": "epsilon",
        "label": "Epsilon (SVR regression only)",
        "type": "number",
        "default": 0.1,
        "range": "0.01 – 1.0",
        "tooltip": "SVR margin width for regression tasks."
      }
    ]
  },
  {
    "algorithmKey": "knn",
    "label": "K-Nearest Neighbors",
    "appliesToText": "classification, regression, and similarity-style tabular baselines — requires feature scaling.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_neighbors",
        "label": "N Neighbors",
        "type": "number",
        "default": 5,
        "range": "3 – 50",
        "required": true,
        "tooltip": "Number of neighbors considered."
      },
      {
        "name": "weights",
        "label": "Weights",
        "type": "select",
        "default": "uniform",
        "options": [
          "uniform",
          "distance"
        ],
        "tooltip": "Neighbor weighting scheme."
      },
      {
        "name": "algorithm",
        "label": "Search Algorithm",
        "type": "select",
        "default": "auto",
        "options": [
          "auto",
          "ball_tree",
          "kd_tree",
          "brute"
        ],
        "tooltip": "Neighbor search algorithm."
      },
      {
        "name": "leaf_size",
        "label": "Leaf Size",
        "type": "number",
        "default": 30,
        "range": "10 – 100",
        "tooltip": "Tree leaf size for ball_tree or kd_tree."
      },
      {
        "name": "p",
        "label": "Distance Power (p)",
        "type": "select",
        "default": "2",
        "options": [
          "1",
          "2"
        ],
        "tooltip": "Distance metric power. 1 = Manhattan, 2 = Euclidean."
      },
      {
        "name": "metric",
        "label": "Distance Metric",
        "type": "select",
        "default": "minkowski",
        "options": [
          "minkowski",
          "euclidean",
          "manhattan"
        ],
        "tooltip": "Distance metric used."
      }
    ]
  },
  {
    "algorithmKey": "naive_bayes",
    "label": "Naive Bayes",
    "appliesToText": "text classification with count/TF-IDF-like features, and simple classification baselines. Gaussian for continuous features, Multinomial/Bernoulli for discrete/text counts.",
    "hasAppliesTo": true,
    "defaultAppliesTo": "gaussian",
    "appliesToTooltip": "Which Naive Bayes variant to train — depends on your feature types.",
    "appliesToOptions": [
      {
        "value": "gaussian",
        "label": "Gaussian"
      },
      {
        "value": "multinomial",
        "label": "Multinomial"
      },
      {
        "value": "bernoulli",
        "label": "Bernoulli"
      }
    ],
    "fields": [
      {
        "name": "var_smoothing",
        "label": "Var Smoothing",
        "type": "number",
        "default": 1e-9,
        "range": "1e-12 – 1e-6",
        "tooltip": "Portion of the largest feature variance added for calculation stability.",
        "showIf": [
          "gaussian"
        ]
      },
      {
        "name": "alpha",
        "label": "Alpha (Smoothing)",
        "type": "number",
        "default": 1,
        "range": "0.01 – 10",
        "required": true,
        "tooltip": "Additive/Laplace smoothing. Higher values smooth probabilities more.",
        "showIf": [
          "multinomial",
          "bernoulli"
        ]
      },
      {
        "name": "fit_prior",
        "label": "Fit Prior",
        "type": "yesno",
        "default": "yes",
        "tooltip": "Whether to learn class prior probabilities.",
        "showIf": [
          "multinomial",
          "bernoulli"
        ]
      },
      {
        "name": "class_prior",
        "label": "Class Prior (blank = learned)",
        "type": "text",
        "default": "",
        "tooltip": "Manual prior probabilities, comma-separated — leave blank to learn from data.",
        "showIf": [
          "multinomial"
        ]
      },
      {
        "name": "binarize",
        "label": "Binarize Threshold",
        "type": "number",
        "default": 0,
        "range": "0.0 – 1.0",
        "tooltip": "Threshold for binarizing feature values before fitting.",
        "showIf": [
          "bernoulli"
        ]
      }
    ]
  },
  {
    "algorithmKey": "decision_tree",
    "label": "Decision Tree",
    "appliesToText": "classification, regression, and interpretable baselines.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "criterion",
        "label": "Criterion",
        "type": "select",
        "default": "gini",
        "options": [
          "gini",
          "entropy",
          "log_loss",
          "squared_error",
          "absolute_error"
        ],
        "tooltip": "Split quality metric."
      },
      {
        "name": "max_depth",
        "label": "Max Depth (blank = full growth)",
        "type": "number",
        "default": 10,
        "range": "3 – 50, or blank",
        "tooltip": "Maximum tree depth."
      },
      {
        "name": "min_samples_split",
        "label": "Min Samples Split",
        "type": "select",
        "default": "2",
        "options": [
          "2",
          "5",
          "10",
          "20"
        ],
        "tooltip": "Minimum samples required to split a node."
      },
      {
        "name": "min_samples_leaf",
        "label": "Min Samples Leaf",
        "type": "select",
        "default": "1",
        "options": [
          "1",
          "2",
          "5",
          "10"
        ],
        "tooltip": "Minimum samples required in a leaf."
      },
      {
        "name": "max_features",
        "label": "Max Features",
        "type": "select",
        "default": "none",
        "options": [
          "none",
          "sqrt",
          "log2",
          "0.8"
        ],
        "tooltip": "Features considered for the best split."
      },
      {
        "name": "max_leaf_nodes",
        "label": "Max Leaf Nodes (blank = unlimited)",
        "type": "number",
        "default": "",
        "range": "10 – 500, or blank",
        "tooltip": "Limits total leaf nodes."
      },
      {
        "name": "class_weight",
        "label": "Class Weight",
        "type": "select",
        "default": "none",
        "options": [
          "none",
          "balanced"
        ],
        "tooltip": "Handles class imbalance."
      }
    ]
  },
  {
    "algorithmKey": "lightgbm",
    "label": "LightGBM",
    "appliesToText": "classification and regression on large tabular datasets, high-cardinality and sparse data.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_estimators",
        "label": "N Estimators",
        "type": "number",
        "default": 1000,
        "range": "100 – 3000",
        "required": true,
        "tooltip": "Number of boosting rounds / trees."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.1,
        "range": "0.005 – 0.2",
        "required": true,
        "tooltip": "Step size. Lower values need more trees."
      },
      {
        "name": "num_leaves",
        "label": "Num Leaves",
        "type": "number",
        "default": 31,
        "range": "16 – 256",
        "tooltip": "Maximum leaves per tree. The primary complexity control."
      },
      {
        "name": "max_depth",
        "label": "Max Depth (-1 = unlimited)",
        "type": "number",
        "default": -1,
        "range": "-1 – 16",
        "tooltip": "Maximum tree depth. -1 means no limit."
      },
      {
        "name": "min_child_samples",
        "label": "Min Child Samples",
        "type": "number",
        "default": 20,
        "range": "10 – 100",
        "tooltip": "Minimum data points in a leaf. Higher values reduce overfit."
      },
      {
        "name": "subsample",
        "label": "Subsample (bagging_fraction)",
        "type": "number",
        "default": 0.8,
        "range": "0.6 – 1.0",
        "tooltip": "Row sampling ratio."
      },
      {
        "name": "colsample_bytree",
        "label": "Colsample by Tree (feature_fraction)",
        "type": "number",
        "default": 0.8,
        "range": "0.6 – 1.0",
        "tooltip": "Feature sampling ratio."
      },
      {
        "name": "reg_alpha",
        "label": "Alpha (L1 Reg)",
        "type": "number",
        "default": 0,
        "range": "0 – 10",
        "tooltip": "L1 regularization."
      },
      {
        "name": "reg_lambda",
        "label": "Lambda (L2 Reg)",
        "type": "number",
        "default": 0,
        "range": "0 – 10",
        "tooltip": "L2 regularization."
      },
      {
        "name": "metric",
        "label": "Metric",
        "type": "select",
        "default": "rmse",
        "options": [
          "rmse",
          "mae",
          "binary_logloss",
          "auc"
        ],
        "tooltip": "Evaluation metric."
      }
    ]
  },
  {
    "algorithmKey": "extra_trees",
    "label": "Extra Trees",
    "appliesToText": "classification and regression — like Random Forest, but splits are chosen at random rather than optimized, trading a little accuracy for speed and extra variance reduction.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_estimators",
        "label": "N Estimators",
        "type": "number",
        "default": 100,
        "range": "10 – 1000",
        "required": true,
        "tooltip": "Number of trees."
      },
      {
        "name": "criterion",
        "label": "Criterion",
        "type": "select",
        "default": "gini",
        "options": [
          "gini",
          "entropy",
          "log_loss",
          "squared_error",
          "absolute_error"
        ],
        "tooltip": "Split quality measure."
      },
      {
        "name": "max_depth",
        "label": "Max Depth (blank = full growth)",
        "type": "number",
        "default": "",
        "range": "5 – 50, or blank",
        "tooltip": "Maximum tree depth."
      },
      {
        "name": "min_samples_split",
        "label": "Min Samples Split",
        "type": "number",
        "default": 2,
        "range": "2 – 20",
        "tooltip": "Minimum samples needed to split a node."
      },
      {
        "name": "min_samples_leaf",
        "label": "Min Samples Leaf",
        "type": "number",
        "default": 1,
        "range": "1 – 10",
        "tooltip": "Minimum samples required at a leaf."
      },
      {
        "name": "max_features",
        "label": "Max Features",
        "type": "select",
        "default": "sqrt",
        "options": [
          "sqrt",
          "log2",
          "0.5",
          "0.8"
        ],
        "tooltip": "Features considered at each split."
      },
      {
        "name": "bootstrap",
        "label": "Bootstrap",
        "type": "yesno",
        "default": "no",
        "tooltip": "Whether trees are trained on bootstrap samples (off by default for Extra Trees)."
      },
      {
        "name": "class_weight",
        "label": "Class Weight",
        "type": "select",
        "default": "none",
        "options": [
          "none",
          "balanced",
          "balanced_subsample"
        ],
        "tooltip": "Handles imbalanced classification."
      }
    ]
  },
  {
    "algorithmKey": "gradient_boosting",
    "label": "Gradient Boosting",
    "appliesToText": "classification and regression — sklearn's native gradient boosting implementation.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "loss",
        "label": "Loss",
        "type": "select",
        "default": "log_loss",
        "options": [
          "log_loss",
          "exponential",
          "squared_error",
          "absolute_error",
          "huber",
          "quantile"
        ],
        "tooltip": "Loss function optimized — classification uses log_loss/exponential, regression uses the others."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.1,
        "range": "0.01 – 0.3",
        "required": true,
        "tooltip": "Shrinks each tree's contribution."
      },
      {
        "name": "n_estimators",
        "label": "N Estimators",
        "type": "number",
        "default": 100,
        "range": "50 – 1000",
        "required": true,
        "tooltip": "Number of boosting stages."
      },
      {
        "name": "subsample",
        "label": "Subsample",
        "type": "number",
        "default": 1,
        "range": "0.5 – 1.0",
        "tooltip": "Fraction of samples used per tree — below 1.0 adds stochastic regularization."
      },
      {
        "name": "max_depth",
        "label": "Max Depth",
        "type": "number",
        "default": 3,
        "range": "2 – 10",
        "tooltip": "Maximum depth of each tree."
      },
      {
        "name": "max_features",
        "label": "Max Features",
        "type": "select",
        "default": "none",
        "options": [
          "none",
          "sqrt",
          "log2"
        ],
        "tooltip": "Features considered at each split."
      }
    ]
  },
  {
    "algorithmKey": "histgradientboosting",
    "label": "HistGradientBoosting",
    "appliesToText": "classification and regression on large tabular datasets — sklearn's histogram-binned boosting, built for speed on bigger data.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.1,
        "range": "0.01 – 0.3",
        "required": true,
        "tooltip": "Shrinks each tree's contribution."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 100,
        "range": "50 – 1000",
        "tooltip": "Number of boosting iterations."
      },
      {
        "name": "max_leaf_nodes",
        "label": "Max Leaf Nodes",
        "type": "number",
        "default": 31,
        "range": "10 – 255",
        "tooltip": "Maximum leaves per tree."
      },
      {
        "name": "max_depth",
        "label": "Max Depth (blank = unlimited)",
        "type": "number",
        "default": "",
        "range": "3 – 50, or blank",
        "tooltip": "Maximum tree depth."
      },
      {
        "name": "min_samples_leaf",
        "label": "Min Samples Leaf",
        "type": "number",
        "default": 20,
        "range": "5 – 100",
        "tooltip": "Minimum samples required at a leaf."
      },
      {
        "name": "l2_regularization",
        "label": "L2 Regularization",
        "type": "number",
        "default": 0,
        "range": "0 – 10",
        "tooltip": "L2 regularization on leaf values."
      },
      {
        "name": "max_bins",
        "label": "Max Bins",
        "type": "number",
        "default": 255,
        "range": "32 – 255",
        "tooltip": "Maximum number of histogram bins per feature."
      }
    ]
  },
  {
    "algorithmKey": "catboost",
    "label": "CatBoost",
    "appliesToText": "classification and regression, especially data with many categorical features — handles them natively without manual one-hot encoding.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "iterations",
        "label": "Iterations",
        "type": "number",
        "default": 1000,
        "range": "100 – 3000",
        "required": true,
        "tooltip": "Number of boosting trees."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.03,
        "range": "0.001 – 0.3",
        "required": true,
        "tooltip": "Step size shrinkage."
      },
      {
        "name": "depth",
        "label": "Depth",
        "type": "number",
        "default": 6,
        "range": "3 – 10",
        "tooltip": "Depth of each tree."
      },
      {
        "name": "l2_leaf_reg",
        "label": "L2 Leaf Reg",
        "type": "number",
        "default": 3,
        "range": "1 – 10",
        "tooltip": "L2 regularization on leaf values."
      },
      {
        "name": "random_strength",
        "label": "Random Strength",
        "type": "number",
        "default": 1,
        "range": "0 – 10",
        "tooltip": "Amount of randomness for scoring splits — helps avoid overfitting."
      },
      {
        "name": "bagging_temperature",
        "label": "Bagging Temperature",
        "type": "number",
        "default": 1,
        "range": "0 – 10",
        "tooltip": "Controls the intensity of Bayesian bagging."
      },
      {
        "name": "border_count",
        "label": "Border Count",
        "type": "number",
        "default": 254,
        "range": "32 – 255",
        "tooltip": "Number of splits considered for numerical features."
      }
    ]
  },
  {
    "algorithmKey": "linear_svm",
    "label": "Linear SVM",
    "appliesToText": "classification on large datasets or high-dimensional sparse features (e.g. text) — faster than kernel SVM since it's restricted to a linear boundary.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "C",
        "label": "C (Regularization)",
        "type": "number",
        "default": 1,
        "range": "0.01 – 100",
        "required": true,
        "tooltip": "Regularization strength. Higher values fit training data more strictly."
      },
      {
        "name": "loss",
        "label": "Loss",
        "type": "select",
        "default": "squared_hinge",
        "options": [
          "hinge",
          "squared_hinge"
        ],
        "tooltip": "Loss function."
      },
      {
        "name": "penalty",
        "label": "Penalty",
        "type": "select",
        "default": "l2",
        "options": [
          "l1",
          "l2"
        ],
        "tooltip": "Regularization norm."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.001",
        "tooltip": "Stopping tolerance."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 1000,
        "range": "100 – 5000",
        "tooltip": "Maximum optimizer iterations."
      },
      {
        "name": "class_weight",
        "label": "Class Weight",
        "type": "select",
        "default": "none",
        "options": [
          "none",
          "balanced"
        ],
        "tooltip": "Handles class imbalance."
      }
    ]
  },
  {
    "algorithmKey": "adaboost",
    "label": "AdaBoost",
    "appliesToText": "classification and regression — combines many weak learners (typically shallow trees), reweighting misclassified examples each round.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_estimators",
        "label": "N Estimators",
        "type": "number",
        "default": 50,
        "range": "10 – 500",
        "required": true,
        "tooltip": "Number of weak learners."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 1,
        "range": "0.01 – 2.0",
        "tooltip": "Shrinks each learner's contribution."
      },
      {
        "name": "algorithm",
        "label": "Algorithm (classifier)",
        "type": "select",
        "default": "SAMME.R",
        "options": [
          "SAMME",
          "SAMME.R"
        ],
        "tooltip": "Boosting algorithm variant — classification only."
      },
      {
        "name": "loss",
        "label": "Loss (regressor)",
        "type": "select",
        "default": "linear",
        "options": [
          "linear",
          "square",
          "exponential"
        ],
        "tooltip": "Loss function used when updating weights — regression only."
      }
    ]
  },
  {
    "algorithmKey": "sgd_classifier",
    "label": "SGD Classifier",
    "appliesToText": "classification on very large datasets — fits via stochastic gradient descent, one sample (or mini-batch) at a time.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "loss",
        "label": "Loss",
        "type": "select",
        "default": "hinge",
        "options": [
          "hinge",
          "log_loss",
          "modified_huber",
          "squared_hinge"
        ],
        "tooltip": "Loss function — hinge gives a linear SVM, log_loss gives logistic regression."
      },
      {
        "name": "penalty",
        "label": "Penalty",
        "type": "select",
        "default": "l2",
        "options": [
          "l2",
          "l1",
          "elasticnet"
        ],
        "tooltip": "Regularization term."
      },
      {
        "name": "alpha",
        "label": "Alpha",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.01",
        "tooltip": "Regularization strength multiplier."
      },
      {
        "name": "l1_ratio",
        "label": "L1 Ratio",
        "type": "number",
        "default": 0.15,
        "range": "0 – 1",
        "tooltip": "Elastic-net mixing ratio — only applies when penalty = elasticnet."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 1000,
        "range": "100 – 5000",
        "tooltip": "Maximum passes over training data."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate Schedule",
        "type": "select",
        "default": "optimal",
        "options": [
          "optimal",
          "constant",
          "invscaling",
          "adaptive"
        ],
        "tooltip": "How the learning rate changes over iterations."
      },
      {
        "name": "eta0",
        "label": "Initial Learning Rate (eta0)",
        "type": "number",
        "default": 0.01,
        "range": "0.001 – 1",
        "tooltip": "Initial learning rate for constant/invscaling/adaptive schedules."
      }
    ]
  },
  {
    "algorithmKey": "mlp",
    "label": "MLP (Neural Network)",
    "appliesToText": "classification and regression — a small fully-connected neural network, a reasonable baseline before reaching for deep learning frameworks.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "hidden_layer_sizes",
        "label": "Hidden Layer Sizes",
        "type": "text",
        "default": "100",
        "tooltip": "Comma-separated layer widths, e.g. '100,50' for two hidden layers."
      },
      {
        "name": "activation",
        "label": "Activation",
        "type": "select",
        "default": "relu",
        "options": [
          "relu",
          "tanh",
          "logistic",
          "identity"
        ],
        "tooltip": "Activation function for hidden layers."
      },
      {
        "name": "solver",
        "label": "Solver",
        "type": "select",
        "default": "adam",
        "options": [
          "adam",
          "sgd",
          "lbfgs"
        ],
        "tooltip": "Weight optimization algorithm."
      },
      {
        "name": "alpha",
        "label": "Alpha (L2 Penalty)",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.01",
        "tooltip": "L2 regularization strength."
      },
      {
        "name": "batch_size",
        "label": "Batch Size",
        "type": "text",
        "default": "auto",
        "tooltip": "Minibatch size for stochastic solvers — 'auto' uses min(200, n_samples)."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate Schedule",
        "type": "select",
        "default": "constant",
        "options": [
          "constant",
          "invscaling",
          "adaptive"
        ],
        "tooltip": "How the learning rate changes over training — sgd solver only."
      },
      {
        "name": "learning_rate_init",
        "label": "Initial Learning Rate",
        "type": "number",
        "default": 0.001,
        "range": "0.0001 – 0.1",
        "tooltip": "Initial learning rate for adam/sgd solvers."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 200,
        "range": "50 – 1000",
        "tooltip": "Maximum training epochs."
      }
    ]
  },
  {
    "algorithmKey": "lda",
    "label": "Linear Discriminant Analysis",
    "appliesToText": "classification and dimensionality reduction — finds the linear combination of features that best separates classes.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "solver",
        "label": "Solver",
        "type": "select",
        "default": "svd",
        "options": [
          "svd",
          "lsqr",
          "eigen"
        ],
        "tooltip": "Solver used — svd doesn't need to compute the covariance matrix, needed for many-feature data."
      },
      {
        "name": "shrinkage",
        "label": "Shrinkage",
        "type": "select",
        "default": "none",
        "options": [
          "none",
          "auto"
        ],
        "tooltip": "Regularizes the covariance estimate — only usable with lsqr/eigen solvers."
      },
      {
        "name": "n_components",
        "label": "N Components (blank = max)",
        "type": "number",
        "default": "",
        "range": "1 – (classes-1), or blank",
        "tooltip": "Number of components to keep for dimensionality reduction."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.001",
        "tooltip": "Threshold for rank estimation in the svd solver."
      }
    ]
  },
  {
    "algorithmKey": "qda",
    "label": "Quadratic Discriminant Analysis",
    "appliesToText": "classification where each class has its own covariance structure — a more flexible boundary than LDA's shared-covariance assumption.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "reg_param",
        "label": "Regularization Param",
        "type": "number",
        "default": 0,
        "range": "0 – 1",
        "tooltip": "Shrinks per-class covariance estimates toward a shared average."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.001",
        "tooltip": "Threshold used for rank estimation."
      }
    ]
  },
  {
    "algorithmKey": "huber_regressor",
    "label": "Huber Regressor",
    "appliesToText": "regression on data with outliers — behaves like linear regression near the bulk of the data but is far less sensitive to extreme values.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "epsilon",
        "label": "Epsilon",
        "type": "number",
        "default": 1.35,
        "range": "1.0 – 2.0",
        "required": true,
        "tooltip": "Threshold where the loss switches from quadratic to linear — smaller values are more robust to outliers."
      },
      {
        "name": "alpha",
        "label": "Alpha (Regularization)",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.01",
        "tooltip": "L2 regularization strength."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 100,
        "range": "50 – 1000",
        "tooltip": "Maximum optimizer iterations."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.00001,
        "range": "0.000001 – 0.0001",
        "tooltip": "Stopping tolerance."
      }
    ]
  },
  {
    "algorithmKey": "bayesian_ridge",
    "label": "Bayesian Ridge",
    "appliesToText": "regression where you want automatic regularization strength selection and predictive uncertainty estimates, not just point predictions.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "alpha_1",
        "label": "Alpha 1",
        "type": "number",
        "default": 0.000001,
        "range": "1e-8 – 1e-4",
        "tooltip": "Shape parameter for the Gamma prior over the noise precision."
      },
      {
        "name": "alpha_2",
        "label": "Alpha 2",
        "type": "number",
        "default": 0.000001,
        "range": "1e-8 – 1e-4",
        "tooltip": "Rate parameter for the Gamma prior over the noise precision."
      },
      {
        "name": "lambda_1",
        "label": "Lambda 1",
        "type": "number",
        "default": 0.000001,
        "range": "1e-8 – 1e-4",
        "tooltip": "Shape parameter for the Gamma prior over the weights precision."
      },
      {
        "name": "lambda_2",
        "label": "Lambda 2",
        "type": "number",
        "default": 0.000001,
        "range": "1e-8 – 1e-4",
        "tooltip": "Rate parameter for the Gamma prior over the weights precision."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.001,
        "range": "0.0001 – 0.01",
        "tooltip": "Stopping tolerance."
      }
    ]
  },
  {
    "algorithmKey": "poisson_regressor",
    "label": "Poisson Regressor",
    "appliesToText": "regression on count data (e.g. number of events) — models a Poisson-distributed target rather than assuming Gaussian errors.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "alpha",
        "label": "Alpha (Regularization)",
        "type": "number",
        "default": 1,
        "range": "0 – 10",
        "required": true,
        "tooltip": "L2 regularization strength."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 100,
        "range": "50 – 1000",
        "tooltip": "Maximum optimizer iterations."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.001",
        "tooltip": "Stopping tolerance."
      }
    ]
  },
  {
    "algorithmKey": "tweedie_regressor",
    "label": "Tweedie Regressor",
    "appliesToText": "regression on skewed, non-negative targets (insurance claims, rainfall) — a generalization covering Poisson, Gamma, and Gaussian as special cases.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "power",
        "label": "Power",
        "type": "number",
        "default": 0,
        "range": "0 – 3",
        "required": true,
        "tooltip": "Tweedie power — 0=Gaussian, 1=Poisson, 2=Gamma, 3=Inverse Gaussian."
      },
      {
        "name": "alpha",
        "label": "Alpha (Regularization)",
        "type": "number",
        "default": 1,
        "range": "0 – 10",
        "tooltip": "L2 regularization strength."
      },
      {
        "name": "link",
        "label": "Link Function",
        "type": "select",
        "default": "auto",
        "options": [
          "auto",
          "identity",
          "log"
        ],
        "tooltip": "Link function connecting the linear model to the target's mean."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 100,
        "range": "50 – 1000",
        "tooltip": "Maximum optimizer iterations."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.001",
        "tooltip": "Stopping tolerance."
      }
    ]
  },
  {
    "algorithmKey": "kmeans",
    "label": "K-Means",
    "appliesToText": "general-purpose clustering when you know roughly how many clusters to expect and they're reasonably spherical.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_clusters",
        "label": "N Clusters",
        "type": "number",
        "default": 8,
        "range": "2 – 50",
        "required": true,
        "tooltip": "Number of clusters to form."
      },
      {
        "name": "init",
        "label": "Init Method",
        "type": "select",
        "default": "k-means++",
        "options": [
          "k-means++",
          "random"
        ],
        "tooltip": "Centroid initialization strategy."
      },
      {
        "name": "n_init",
        "label": "N Init",
        "type": "text",
        "default": "auto",
        "tooltip": "Number of times to run with different centroid seeds; 'auto' picks a sensible count."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 300,
        "range": "100 – 1000",
        "tooltip": "Maximum iterations for a single run."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.001",
        "tooltip": "Convergence tolerance based on centroid movement."
      },
      {
        "name": "algorithm",
        "label": "Algorithm",
        "type": "select",
        "default": "lloyd",
        "options": [
          "lloyd",
          "elkan"
        ],
        "tooltip": "K-Means variant — elkan can be faster on well-separated clusters."
      }
    ]
  },
  {
    "algorithmKey": "dbscan",
    "label": "DBSCAN",
    "appliesToText": "density-based clustering that finds arbitrarily-shaped clusters and naturally labels sparse points as noise — no need to pre-specify cluster count.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "eps",
        "label": "Epsilon (eps)",
        "type": "number",
        "default": 0.5,
        "range": "0.01 – 5",
        "required": true,
        "tooltip": "Maximum distance between two samples for one to be a neighbor of the other."
      },
      {
        "name": "min_samples",
        "label": "Min Samples",
        "type": "number",
        "default": 5,
        "range": "2 – 50",
        "required": true,
        "tooltip": "Minimum neighbors for a point to be a core point."
      },
      {
        "name": "metric",
        "label": "Metric",
        "type": "select",
        "default": "euclidean",
        "options": [
          "euclidean",
          "manhattan",
          "cosine"
        ],
        "tooltip": "Distance metric."
      },
      {
        "name": "algorithm",
        "label": "Algorithm",
        "type": "select",
        "default": "auto",
        "options": [
          "auto",
          "ball_tree",
          "kd_tree",
          "brute"
        ],
        "tooltip": "Nearest-neighbor search algorithm."
      }
    ]
  },
  {
    "algorithmKey": "agglomerative_clustering",
    "label": "Agglomerative Clustering",
    "appliesToText": "hierarchical clustering using a bottom-up approach — useful when you want a dendrogram or don't know the cluster count in advance.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_clusters",
        "label": "N Clusters",
        "type": "number",
        "default": 2,
        "range": "2 – 50",
        "required": true,
        "tooltip": "Number of clusters to find."
      },
      {
        "name": "linkage",
        "label": "Linkage",
        "type": "select",
        "default": "ward",
        "options": [
          "ward",
          "complete",
          "average",
          "single"
        ],
        "tooltip": "Which distance to use between sets of observations."
      },
      {
        "name": "metric",
        "label": "Metric",
        "type": "select",
        "default": "euclidean",
        "options": [
          "euclidean",
          "manhattan",
          "cosine"
        ],
        "tooltip": "Distance metric — ward linkage requires euclidean."
      }
    ]
  },
  {
    "algorithmKey": "gaussian_mixture",
    "label": "Gaussian Mixture",
    "appliesToText": "soft clustering where each point gets a probability of belonging to each cluster, and clusters can be elliptical, not just spherical.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_components",
        "label": "N Components",
        "type": "number",
        "default": 1,
        "range": "1 – 20",
        "required": true,
        "tooltip": "Number of mixture components (clusters)."
      },
      {
        "name": "covariance_type",
        "label": "Covariance Type",
        "type": "select",
        "default": "full",
        "options": [
          "full",
          "tied",
          "diag",
          "spherical"
        ],
        "tooltip": "Shape constraint on each component's covariance."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 100,
        "range": "50 – 500",
        "tooltip": "Maximum EM algorithm iterations."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.001,
        "range": "0.0001 – 0.01",
        "tooltip": "Convergence threshold."
      },
      {
        "name": "init_params",
        "label": "Init Params",
        "type": "select",
        "default": "kmeans",
        "options": [
          "kmeans",
          "random"
        ],
        "tooltip": "Method used to initialize weights/means/covariances."
      }
    ]
  },
  {
    "algorithmKey": "mean_shift",
    "label": "Mean Shift",
    "appliesToText": "clustering that discovers the number of clusters automatically by locating density peaks — no need to specify a cluster count.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "bandwidth",
        "label": "Bandwidth (blank = auto-estimate)",
        "type": "text",
        "default": "",
        "tooltip": "Kernel bandwidth — leave blank to estimate automatically from the data."
      },
      {
        "name": "bin_seeding",
        "label": "Bin Seeding",
        "type": "yesno",
        "default": "no",
        "tooltip": "Speeds up initialization by binning points onto a grid first."
      },
      {
        "name": "cluster_all",
        "label": "Cluster All Points",
        "type": "yesno",
        "default": "yes",
        "tooltip": "If no, orphan points outside any kernel are labeled as noise (-1)."
      }
    ]
  },
  {
    "algorithmKey": "spectral_clustering",
    "label": "Spectral Clustering",
    "appliesToText": "clustering non-convex shapes by first embedding points using graph structure, then clustering the embedding.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_clusters",
        "label": "N Clusters",
        "type": "number",
        "default": 8,
        "range": "2 – 50",
        "required": true,
        "tooltip": "Number of clusters to find."
      },
      {
        "name": "affinity",
        "label": "Affinity",
        "type": "select",
        "default": "rbf",
        "options": [
          "rbf",
          "nearest_neighbors",
          "precomputed"
        ],
        "tooltip": "How to construct the affinity matrix."
      },
      {
        "name": "gamma",
        "label": "Gamma",
        "type": "number",
        "default": 1,
        "range": "0.01 – 10",
        "tooltip": "Kernel coefficient for the rbf affinity."
      },
      {
        "name": "n_neighbors",
        "label": "N Neighbors",
        "type": "number",
        "default": 10,
        "range": "5 – 50",
        "tooltip": "Neighbors used when affinity = nearest_neighbors."
      }
    ]
  },
  {
    "algorithmKey": "birch",
    "label": "BIRCH",
    "appliesToText": "clustering very large datasets that don't fit comfortably in memory — builds a compact tree summary first, then clusters that.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "threshold",
        "label": "Threshold",
        "type": "number",
        "default": 0.5,
        "range": "0.1 – 2",
        "required": true,
        "tooltip": "Max radius of a subcluster before it's split."
      },
      {
        "name": "branching_factor",
        "label": "Branching Factor",
        "type": "number",
        "default": 50,
        "range": "10 – 200",
        "tooltip": "Max children per tree node."
      },
      {
        "name": "n_clusters",
        "label": "N Clusters (blank = keep subclusters)",
        "type": "number",
        "default": 3,
        "range": "2 – 50, or blank",
        "tooltip": "Final clustering step applied to the subcluster centroids."
      }
    ]
  },
  {
    "algorithmKey": "isolation_forest",
    "label": "Isolation Forest",
    "appliesToText": "general-purpose anomaly detection — isolates outliers with fewer random splits than normal points need, without assuming a data distribution.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_estimators",
        "label": "N Estimators",
        "type": "number",
        "default": 100,
        "range": "50 – 500",
        "required": true,
        "tooltip": "Number of isolation trees."
      },
      {
        "name": "max_samples",
        "label": "Max Samples",
        "type": "text",
        "default": "auto",
        "tooltip": "Samples drawn per tree — 'auto' uses min(256, n_samples)."
      },
      {
        "name": "contamination",
        "label": "Contamination",
        "type": "text",
        "default": "auto",
        "tooltip": "Expected proportion of outliers — 'auto' or a fraction like 0.05."
      },
      {
        "name": "max_features",
        "label": "Max Features",
        "type": "number",
        "default": 1,
        "range": "0.1 – 1.0",
        "tooltip": "Fraction of features drawn per tree."
      },
      {
        "name": "bootstrap",
        "label": "Bootstrap",
        "type": "yesno",
        "default": "no",
        "tooltip": "Whether samples are drawn with replacement."
      }
    ]
  },
  {
    "algorithmKey": "local_outlier_factor",
    "label": "Local Outlier Factor",
    "appliesToText": "detecting anomalies that are only unusual relative to their local neighborhood, in data with clusters of varying density.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_neighbors",
        "label": "N Neighbors",
        "type": "number",
        "default": 20,
        "range": "5 – 100",
        "required": true,
        "tooltip": "Neighbors used to compute local density."
      },
      {
        "name": "algorithm",
        "label": "Algorithm",
        "type": "select",
        "default": "auto",
        "options": [
          "auto",
          "ball_tree",
          "kd_tree",
          "brute"
        ],
        "tooltip": "Nearest-neighbor search algorithm."
      },
      {
        "name": "leaf_size",
        "label": "Leaf Size",
        "type": "number",
        "default": 30,
        "range": "10 – 100",
        "tooltip": "Tree leaf size for ball_tree/kd_tree."
      },
      {
        "name": "metric",
        "label": "Metric",
        "type": "select",
        "default": "minkowski",
        "options": [
          "minkowski",
          "euclidean",
          "manhattan"
        ],
        "tooltip": "Distance metric."
      },
      {
        "name": "contamination",
        "label": "Contamination",
        "type": "text",
        "default": "auto",
        "tooltip": "Expected proportion of outliers."
      }
    ]
  },
  {
    "algorithmKey": "one_class_svm",
    "label": "One-Class SVM",
    "appliesToText": "anomaly detection with complex, non-linear decision boundaries — fits a boundary around the 'normal' data using the SVM kernel trick.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "kernel",
        "label": "Kernel",
        "type": "select",
        "default": "rbf",
        "options": [
          "rbf",
          "linear",
          "poly",
          "sigmoid"
        ],
        "tooltip": "Kernel type."
      },
      {
        "name": "degree",
        "label": "Degree (poly only)",
        "type": "number",
        "default": 3,
        "range": "2 – 5",
        "tooltip": "Polynomial degree — only for the poly kernel."
      },
      {
        "name": "gamma",
        "label": "Gamma",
        "type": "select",
        "default": "scale",
        "options": [
          "scale",
          "auto"
        ],
        "tooltip": "Kernel coefficient for rbf/poly/sigmoid."
      },
      {
        "name": "nu",
        "label": "Nu",
        "type": "number",
        "default": 0.5,
        "range": "0.01 – 1.0",
        "required": true,
        "tooltip": "Upper bound on the fraction of outliers, lower bound on support vectors."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.001,
        "range": "0.00001 – 0.001",
        "tooltip": "Stopping tolerance."
      }
    ]
  },
  {
    "algorithmKey": "elliptic_envelope",
    "label": "Elliptic Envelope",
    "appliesToText": "anomaly detection when the normal data is roughly Gaussian-distributed — fits an ellipse around the bulk of the data.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "contamination",
        "label": "Contamination",
        "type": "number",
        "default": 0.1,
        "range": "0.01 – 0.5",
        "required": true,
        "tooltip": "Expected proportion of outliers."
      },
      {
        "name": "support_fraction",
        "label": "Support Fraction (blank = auto)",
        "type": "text",
        "default": "",
        "tooltip": "Fraction of points used to estimate the covariance — leave blank to let sklearn choose."
      }
    ]
  },
  {
    "algorithmKey": "autoencoder",
    "label": "Autoencoder",
    "appliesToText": "anomaly detection on complex, high-dimensional data — flags points the network reconstructs poorly as anomalies.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "hidden_layers",
        "label": "Hidden Layers",
        "type": "text",
        "default": "128,64,32",
        "tooltip": "Comma-separated encoder layer widths (decoder mirrors it)."
      },
      {
        "name": "latent_dim",
        "label": "Latent Dimension",
        "type": "number",
        "default": 16,
        "range": "2 – 256",
        "required": true,
        "tooltip": "Size of the compressed bottleneck representation."
      },
      {
        "name": "activation",
        "label": "Activation",
        "type": "select",
        "default": "relu",
        "options": [
          "relu",
          "tanh"
        ],
        "tooltip": "Activation function for hidden layers."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.001,
        "range": "0.0001 – 0.1",
        "tooltip": "Optimizer learning rate."
      },
      {
        "name": "batch_size",
        "label": "Batch Size",
        "type": "number",
        "default": 32,
        "range": "8 – 256",
        "tooltip": "Training batch size."
      },
      {
        "name": "epochs",
        "label": "Epochs",
        "type": "number",
        "default": 50,
        "range": "10 – 500",
        "tooltip": "Full passes over the training data."
      },
      {
        "name": "dropout",
        "label": "Dropout",
        "type": "number",
        "default": 0.2,
        "range": "0 – 0.5",
        "tooltip": "Dropout rate for regularization."
      }
    ]
  },
  {
    "algorithmKey": "lstm_autoencoder",
    "label": "LSTM Autoencoder",
    "appliesToText": "anomaly detection in time-series — learns to reconstruct normal sequences, flags sequences with high reconstruction error.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "sequence_length",
        "label": "Sequence Length",
        "type": "number",
        "default": 30,
        "range": "5 – 200",
        "required": true,
        "tooltip": "Number of timesteps per input window."
      },
      {
        "name": "lstm_units",
        "label": "LSTM Units",
        "type": "number",
        "default": 64,
        "range": "8 – 512",
        "tooltip": "Hidden units per LSTM layer."
      },
      {
        "name": "latent_dim",
        "label": "Latent Dimension",
        "type": "number",
        "default": 16,
        "range": "2 – 256",
        "tooltip": "Size of the compressed sequence representation."
      },
      {
        "name": "dropout",
        "label": "Dropout",
        "type": "number",
        "default": 0.2,
        "range": "0 – 0.5",
        "tooltip": "Dropout rate for regularization."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.001,
        "range": "0.0001 – 0.1",
        "tooltip": "Optimizer learning rate."
      },
      {
        "name": "batch_size",
        "label": "Batch Size",
        "type": "number",
        "default": 32,
        "range": "8 – 256",
        "tooltip": "Training batch size."
      },
      {
        "name": "epochs",
        "label": "Epochs",
        "type": "number",
        "default": 50,
        "range": "10 – 500",
        "tooltip": "Full passes over the training data."
      }
    ]
  },
  {
    "algorithmKey": "arima",
    "label": "ARIMA",
    "appliesToText": "univariate time-series forecasting with trend but no seasonality — the classic Box-Jenkins model.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "p",
        "label": "p (AR order)",
        "type": "number",
        "default": 1,
        "range": "0 – 10",
        "required": true,
        "tooltip": "Number of autoregressive terms."
      },
      {
        "name": "d",
        "label": "d (Differencing)",
        "type": "number",
        "default": 1,
        "range": "0 – 2",
        "required": true,
        "tooltip": "Number of differences needed to make the series stationary."
      },
      {
        "name": "q",
        "label": "q (MA order)",
        "type": "number",
        "default": 1,
        "range": "0 – 10",
        "required": true,
        "tooltip": "Number of moving-average terms."
      }
    ]
  },
  {
    "algorithmKey": "sarima",
    "label": "SARIMA",
    "appliesToText": "time-series forecasting with both trend and seasonality (e.g. weekly or yearly cycles).",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "p",
        "label": "p (AR order)",
        "type": "number",
        "default": 1,
        "range": "0 – 10",
        "tooltip": "Non-seasonal autoregressive order."
      },
      {
        "name": "d",
        "label": "d (Differencing)",
        "type": "number",
        "default": 1,
        "range": "0 – 2",
        "tooltip": "Non-seasonal differencing order."
      },
      {
        "name": "q",
        "label": "q (MA order)",
        "type": "number",
        "default": 1,
        "range": "0 – 10",
        "tooltip": "Non-seasonal moving-average order."
      },
      {
        "name": "P",
        "label": "P (Seasonal AR)",
        "type": "number",
        "default": 1,
        "range": "0 – 5",
        "tooltip": "Seasonal autoregressive order."
      },
      {
        "name": "D",
        "label": "D (Seasonal Diff)",
        "type": "number",
        "default": 1,
        "range": "0 – 2",
        "tooltip": "Seasonal differencing order."
      },
      {
        "name": "Q",
        "label": "Q (Seasonal MA)",
        "type": "number",
        "default": 1,
        "range": "0 – 5",
        "tooltip": "Seasonal moving-average order."
      },
      {
        "name": "seasonal_period",
        "label": "Seasonal Period",
        "type": "number",
        "default": 12,
        "range": "2 – 365",
        "required": true,
        "tooltip": "Number of timesteps per seasonal cycle (12 for monthly/yearly)."
      }
    ]
  },
  {
    "algorithmKey": "auto_arima",
    "label": "Auto-ARIMA",
    "appliesToText": "the same model family as SARIMA, but automatically searches for the best (p,d,q)(P,D,Q) orders instead of you specifying them.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "start_p",
        "label": "Start p",
        "type": "number",
        "default": 2,
        "range": "0 – 10",
        "tooltip": "Starting AR order for the search."
      },
      {
        "name": "max_p",
        "label": "Max p",
        "type": "number",
        "default": 5,
        "range": "1 – 20",
        "tooltip": "Upper bound on AR order."
      },
      {
        "name": "start_q",
        "label": "Start q",
        "type": "number",
        "default": 2,
        "range": "0 – 10",
        "tooltip": "Starting MA order for the search."
      },
      {
        "name": "max_q",
        "label": "Max q",
        "type": "number",
        "default": 5,
        "range": "1 – 20",
        "tooltip": "Upper bound on MA order."
      },
      {
        "name": "max_d",
        "label": "Max d",
        "type": "number",
        "default": 2,
        "range": "0 – 3",
        "tooltip": "Upper bound on differencing order."
      },
      {
        "name": "seasonal",
        "label": "Seasonal",
        "type": "yesno",
        "default": "yes",
        "tooltip": "Whether to search seasonal orders too."
      },
      {
        "name": "m",
        "label": "Seasonal Period (m)",
        "type": "number",
        "default": 12,
        "range": "1 – 365",
        "tooltip": "Number of timesteps per seasonal cycle."
      }
    ]
  },
  {
    "algorithmKey": "exponential_smoothing",
    "label": "Exponential Smoothing",
    "appliesToText": "simple, fast forecasting with optional trend and seasonality — a strong baseline before reaching for ARIMA or ML-based forecasters.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "trend",
        "label": "Trend",
        "type": "select",
        "default": "add",
        "options": [
          "add",
          "mul",
          "none"
        ],
        "tooltip": "Trend component type."
      },
      {
        "name": "seasonal",
        "label": "Seasonal",
        "type": "select",
        "default": "add",
        "options": [
          "add",
          "mul",
          "none"
        ],
        "tooltip": "Seasonal component type."
      },
      {
        "name": "seasonal_periods",
        "label": "Seasonal Periods",
        "type": "number",
        "default": 12,
        "range": "2 – 365",
        "tooltip": "Timesteps per seasonal cycle."
      },
      {
        "name": "damped_trend",
        "label": "Damped Trend",
        "type": "yesno",
        "default": "no",
        "tooltip": "Flattens the trend's long-run forecast instead of extrapolating it indefinitely."
      }
    ]
  },
  {
    "algorithmKey": "holt_winters",
    "label": "Holt-Winters",
    "appliesToText": "triple exponential smoothing — level, trend, and seasonality together, the most complete member of the exponential smoothing family.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "trend",
        "label": "Trend",
        "type": "select",
        "default": "add",
        "options": [
          "add",
          "mul",
          "none"
        ],
        "tooltip": "Trend component type."
      },
      {
        "name": "seasonal",
        "label": "Seasonal",
        "type": "select",
        "default": "add",
        "options": [
          "add",
          "mul",
          "none"
        ],
        "tooltip": "Seasonal component type."
      },
      {
        "name": "seasonal_periods",
        "label": "Seasonal Periods",
        "type": "number",
        "default": 12,
        "range": "2 – 365",
        "required": true,
        "tooltip": "Timesteps per seasonal cycle."
      },
      {
        "name": "damped_trend",
        "label": "Damped Trend",
        "type": "yesno",
        "default": "no",
        "tooltip": "Flattens the trend's long-run forecast instead of extrapolating it indefinitely."
      }
    ]
  },
  {
    "algorithmKey": "prophet",
    "label": "Prophet",
    "appliesToText": "business forecasting with holidays, missing data, and multiple seasonalities — tuned to need little manual tuning.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "changepoint_prior_scale",
        "label": "Changepoint Prior Scale",
        "type": "number",
        "default": 0.05,
        "range": "0.001 – 0.5",
        "required": true,
        "tooltip": "Flexibility of the trend — higher values fit trend changes more aggressively."
      },
      {
        "name": "seasonality_prior_scale",
        "label": "Seasonality Prior Scale",
        "type": "number",
        "default": 10,
        "range": "0.01 – 10",
        "tooltip": "Strength of the seasonality model."
      },
      {
        "name": "holidays_prior_scale",
        "label": "Holidays Prior Scale",
        "type": "number",
        "default": 10,
        "range": "0.01 – 10",
        "tooltip": "Strength of the holiday effects model."
      },
      {
        "name": "seasonality_mode",
        "label": "Seasonality Mode",
        "type": "select",
        "default": "additive",
        "options": [
          "additive",
          "multiplicative"
        ],
        "tooltip": "Whether seasonal effects add to or multiply the trend."
      },
      {
        "name": "changepoint_range",
        "label": "Changepoint Range",
        "type": "number",
        "default": 0.8,
        "range": "0.5 – 0.95",
        "tooltip": "Proportion of history where trend changepoints are considered."
      }
    ]
  },
  {
    "algorithmKey": "lstm_forecaster",
    "label": "LSTM Forecaster",
    "appliesToText": "forecasting with long-range dependencies or multiple correlated series — a recurrent network trained on windowed sequences.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "sequence_length",
        "label": "Sequence Length",
        "type": "number",
        "default": 30,
        "range": "5 – 200",
        "required": true,
        "tooltip": "Number of past timesteps used per prediction."
      },
      {
        "name": "lstm_units",
        "label": "LSTM Units",
        "type": "number",
        "default": 64,
        "range": "8 – 512",
        "tooltip": "Hidden units per LSTM layer."
      },
      {
        "name": "num_layers",
        "label": "Num Layers",
        "type": "number",
        "default": 2,
        "range": "1 – 5",
        "tooltip": "Number of stacked LSTM layers."
      },
      {
        "name": "dropout",
        "label": "Dropout",
        "type": "number",
        "default": 0.2,
        "range": "0 – 0.5",
        "tooltip": "Dropout rate for regularization."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.001,
        "range": "0.0001 – 0.1",
        "tooltip": "Optimizer learning rate."
      },
      {
        "name": "batch_size",
        "label": "Batch Size",
        "type": "number",
        "default": 32,
        "range": "8 – 256",
        "tooltip": "Training batch size."
      },
      {
        "name": "epochs",
        "label": "Epochs",
        "type": "number",
        "default": 50,
        "range": "10 – 500",
        "tooltip": "Full passes over the training data."
      }
    ]
  },
  {
    "algorithmKey": "gru_forecaster",
    "label": "GRU Forecaster",
    "appliesToText": "similar to LSTM forecasting with fewer parameters and often comparable accuracy — a lighter-weight recurrent option.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "sequence_length",
        "label": "Sequence Length",
        "type": "number",
        "default": 30,
        "range": "5 – 200",
        "required": true,
        "tooltip": "Number of past timesteps used per prediction."
      },
      {
        "name": "gru_units",
        "label": "GRU Units",
        "type": "number",
        "default": 64,
        "range": "8 – 512",
        "tooltip": "Hidden units per GRU layer."
      },
      {
        "name": "num_layers",
        "label": "Num Layers",
        "type": "number",
        "default": 2,
        "range": "1 – 5",
        "tooltip": "Number of stacked GRU layers."
      },
      {
        "name": "dropout",
        "label": "Dropout",
        "type": "number",
        "default": 0.2,
        "range": "0 – 0.5",
        "tooltip": "Dropout rate for regularization."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.001,
        "range": "0.0001 – 0.1",
        "tooltip": "Optimizer learning rate."
      },
      {
        "name": "batch_size",
        "label": "Batch Size",
        "type": "number",
        "default": 32,
        "range": "8 – 256",
        "tooltip": "Training batch size."
      },
      {
        "name": "epochs",
        "label": "Epochs",
        "type": "number",
        "default": 50,
        "range": "10 – 500",
        "tooltip": "Full passes over the training data."
      }
    ]
  },
  {
    "algorithmKey": "tft",
    "label": "Temporal Fusion Transformer",
    "appliesToText": "forecasting with many known future inputs (prices, promotions) and a need for interpretable attention over which timesteps mattered.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "hidden_size",
        "label": "Hidden Size",
        "type": "number",
        "default": 16,
        "range": "8 – 256",
        "required": true,
        "tooltip": "Width of the model's internal representations."
      },
      {
        "name": "attention_head_size",
        "label": "Attention Head Size",
        "type": "number",
        "default": 4,
        "range": "1 – 16",
        "tooltip": "Number of attention heads."
      },
      {
        "name": "dropout",
        "label": "Dropout",
        "type": "number",
        "default": 0.1,
        "range": "0 – 0.5",
        "tooltip": "Dropout rate for regularization."
      },
      {
        "name": "hidden_continuous_size",
        "label": "Hidden Continuous Size",
        "type": "number",
        "default": 8,
        "range": "4 – 128",
        "tooltip": "Hidden size for processing continuous variables."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.001,
        "range": "0.0001 – 0.1",
        "tooltip": "Optimizer learning rate."
      }
    ]
  },
  {
    "algorithmKey": "nbeats",
    "label": "N-BEATS",
    "appliesToText": "pure deep-learning forecasting with no hand-crafted seasonal features — a stack of fully-connected blocks that learns its own basis functions.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "input_chunk_length",
        "label": "Input Chunk Length",
        "type": "number",
        "default": 30,
        "range": "10 – 200",
        "required": true,
        "tooltip": "Number of past timesteps fed to the model."
      },
      {
        "name": "output_chunk_length",
        "label": "Output Chunk Length",
        "type": "number",
        "default": 7,
        "range": "1 – 100",
        "required": true,
        "tooltip": "Number of future timesteps predicted per forward pass."
      },
      {
        "name": "num_stacks",
        "label": "Num Stacks",
        "type": "number",
        "default": 2,
        "range": "1 – 10",
        "tooltip": "Number of stacks in the architecture."
      },
      {
        "name": "num_blocks",
        "label": "Num Blocks",
        "type": "number",
        "default": 3,
        "range": "1 – 10",
        "tooltip": "Number of blocks per stack."
      },
      {
        "name": "num_layers",
        "label": "Num Layers",
        "type": "number",
        "default": 4,
        "range": "1 – 10",
        "tooltip": "Fully-connected layers per block."
      },
      {
        "name": "layer_widths",
        "label": "Layer Widths",
        "type": "number",
        "default": 256,
        "range": "32 – 512",
        "tooltip": "Width of each fully-connected layer."
      }
    ]
  },
  {
    "algorithmKey": "pca",
    "label": "PCA",
    "appliesToText": "linear dimensionality reduction and denoising — projects onto the directions of maximum variance.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_components",
        "label": "N Components",
        "type": "text",
        "default": "2",
        "tooltip": "Number of components to keep, or a float in (0,1) to keep that much variance."
      },
      {
        "name": "whiten",
        "label": "Whiten",
        "type": "yesno",
        "default": "no",
        "tooltip": "Scales components to unit variance — can help downstream estimators."
      },
      {
        "name": "svd_solver",
        "label": "SVD Solver",
        "type": "select",
        "default": "auto",
        "options": [
          "auto",
          "full",
          "arpack",
          "randomized"
        ],
        "tooltip": "Solver used to compute the decomposition."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0,
        "range": "0 – 1",
        "tooltip": "Tolerance for the arpack solver's singular values."
      }
    ]
  },
  {
    "algorithmKey": "incremental_pca",
    "label": "Incremental PCA",
    "appliesToText": "PCA on datasets too large to fit in memory at once — processes data in mini-batches.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_components",
        "label": "N Components",
        "type": "number",
        "default": 2,
        "range": "1 – 500",
        "required": true,
        "tooltip": "Number of components to keep."
      },
      {
        "name": "whiten",
        "label": "Whiten",
        "type": "yesno",
        "default": "no",
        "tooltip": "Scales components to unit variance."
      },
      {
        "name": "batch_size",
        "label": "Batch Size (blank = auto)",
        "type": "text",
        "default": "",
        "tooltip": "Samples per batch — leave blank to use 5x n_features."
      }
    ]
  },
  {
    "algorithmKey": "kernel_pca",
    "label": "Kernel PCA",
    "appliesToText": "non-linear dimensionality reduction — applies the kernel trick so PCA can capture curved structure, not just linear directions.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_components",
        "label": "N Components",
        "type": "number",
        "default": 2,
        "range": "1 – 500",
        "required": true,
        "tooltip": "Number of components to keep."
      },
      {
        "name": "kernel",
        "label": "Kernel",
        "type": "select",
        "default": "linear",
        "options": [
          "linear",
          "poly",
          "rbf",
          "sigmoid",
          "cosine"
        ],
        "tooltip": "Kernel used for the embedding."
      },
      {
        "name": "gamma",
        "label": "Gamma (blank = auto)",
        "type": "text",
        "default": "",
        "tooltip": "Kernel coefficient for rbf/poly/sigmoid — leave blank for 1/n_features."
      },
      {
        "name": "degree",
        "label": "Degree (poly only)",
        "type": "number",
        "default": 3,
        "range": "2 – 5",
        "tooltip": "Polynomial degree — only for the poly kernel."
      },
      {
        "name": "coef0",
        "label": "Coef0 (poly/sigmoid)",
        "type": "number",
        "default": 1,
        "range": "0 – 5",
        "tooltip": "Independent term for poly and sigmoid kernels."
      }
    ]
  },
  {
    "algorithmKey": "truncated_svd",
    "label": "Truncated SVD",
    "appliesToText": "dimensionality reduction on sparse data (e.g. TF-IDF text features) — PCA without centering, so sparsity is preserved.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_components",
        "label": "N Components",
        "type": "number",
        "default": 2,
        "range": "1 – 500",
        "required": true,
        "tooltip": "Number of components to keep."
      },
      {
        "name": "algorithm",
        "label": "Algorithm",
        "type": "select",
        "default": "randomized",
        "options": [
          "randomized",
          "arpack"
        ],
        "tooltip": "Solver used to compute the decomposition."
      },
      {
        "name": "n_iter",
        "label": "N Iterations",
        "type": "number",
        "default": 5,
        "range": "1 – 20",
        "tooltip": "Iterations for the randomized solver."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0,
        "range": "0 – 1",
        "tooltip": "Tolerance for the arpack solver."
      }
    ]
  },
  {
    "algorithmKey": "fastica",
    "label": "FastICA",
    "appliesToText": "separating a multivariate signal into statistically independent components — useful for signal/source separation, not just variance-based reduction.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_components",
        "label": "N Components (blank = all)",
        "type": "text",
        "default": "",
        "tooltip": "Number of independent components to extract."
      },
      {
        "name": "algorithm",
        "label": "Algorithm",
        "type": "select",
        "default": "parallel",
        "options": [
          "parallel",
          "deflation"
        ],
        "tooltip": "Whether components are extracted simultaneously or one at a time."
      },
      {
        "name": "whiten",
        "label": "Whiten",
        "type": "select",
        "default": "unit-variance",
        "options": [
          "unit-variance",
          "arbitrary-variance",
          "false"
        ],
        "tooltip": "Whitening strategy applied before unmixing."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 200,
        "range": "50 – 1000",
        "tooltip": "Maximum iterations during fitting."
      },
      {
        "name": "tol",
        "label": "Tolerance",
        "type": "number",
        "default": 0.0001,
        "range": "0.00001 – 0.001",
        "tooltip": "Convergence tolerance."
      }
    ]
  },
  {
    "algorithmKey": "tsne",
    "label": "t-SNE",
    "appliesToText": "2D/3D visualization of high-dimensional data — preserves local neighborhood structure, not global distances, so it's for visualization, not general feature reduction.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_components",
        "label": "N Components",
        "type": "number",
        "default": 2,
        "range": "2 – 3",
        "required": true,
        "tooltip": "Dimension of the embedded space — almost always 2 or 3, for plotting."
      },
      {
        "name": "perplexity",
        "label": "Perplexity",
        "type": "number",
        "default": 30,
        "range": "5 – 50",
        "required": true,
        "tooltip": "Roughly, the number of nearest neighbors considered per point."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "text",
        "default": "auto",
        "tooltip": "Optimizer learning rate — 'auto' scales it with dataset size."
      },
      {
        "name": "max_iter",
        "label": "Max Iterations",
        "type": "number",
        "default": 1000,
        "range": "250 – 5000",
        "tooltip": "Maximum optimization iterations."
      },
      {
        "name": "metric",
        "label": "Metric",
        "type": "select",
        "default": "euclidean",
        "options": [
          "euclidean",
          "cosine",
          "manhattan"
        ],
        "tooltip": "Distance metric used to compute neighborhoods."
      }
    ]
  },
  {
    "algorithmKey": "umap",
    "label": "UMAP",
    "appliesToText": "faster, more scalable alternative to t-SNE that better preserves global structure — usable for both visualization and general-purpose reduction.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "n_neighbors",
        "label": "N Neighbors",
        "type": "number",
        "default": 15,
        "range": "2 – 200",
        "required": true,
        "tooltip": "Local neighborhood size — larger values preserve more global structure."
      },
      {
        "name": "n_components",
        "label": "N Components",
        "type": "number",
        "default": 2,
        "range": "2 – 100",
        "tooltip": "Dimension of the embedded space."
      },
      {
        "name": "min_dist",
        "label": "Min Distance",
        "type": "number",
        "default": 0.1,
        "range": "0 – 1",
        "tooltip": "Minimum distance between points in the embedding — controls how tightly points cluster."
      },
      {
        "name": "metric",
        "label": "Metric",
        "type": "select",
        "default": "euclidean",
        "options": [
          "euclidean",
          "cosine",
          "manhattan"
        ],
        "tooltip": "Distance metric used in the original space."
      },
      {
        "name": "spread",
        "label": "Spread",
        "type": "number",
        "default": 1,
        "range": "0.5 – 5",
        "tooltip": "Effective scale of embedded points, works with min_dist."
      }
    ]
  },
  {
    "algorithmKey": "deep_mlp",
    "label": "Deep MLP (PyTorch)",
    "appliesToText": "classification and regression — a deeper multi-layer feedforward network than the sklearn MLP above, trained with backprop on GPU.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "hidden_layers",
        "label": "Hidden Layers",
        "type": "text",
        "default": "256,128,64",
        "tooltip": "Comma-separated layer widths — deeper than sklearn's mlp is worth reaching for once a shallow MLP underfits."
      },
      {
        "name": "activation",
        "label": "Activation",
        "type": "select",
        "default": "relu",
        "options": [
          "relu",
          "gelu",
          "tanh"
        ],
        "tooltip": "Activation function for hidden layers."
      },
      {
        "name": "dropout",
        "label": "Dropout",
        "type": "number",
        "default": 0.1,
        "range": "0 – 0.5",
        "tooltip": "Dropout probability between hidden layers, for regularization."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.001,
        "range": "0.00001 – 0.1",
        "tooltip": "Optimizer learning rate."
      },
      {
        "name": "batch_size",
        "label": "Batch Size",
        "type": "number",
        "default": 64,
        "range": "8 – 512",
        "tooltip": "Minibatch size."
      },
      {
        "name": "epochs",
        "label": "Epochs",
        "type": "number",
        "default": 30,
        "range": "1 – 200",
        "tooltip": "Number of full passes over the training data."
      }
    ]
  },
  {
    "algorithmKey": "cnn",
    "label": "CNN (PyTorch)",
    "appliesToText": "image classification — the only base model in the library built for pixel input rather than tabular or sequence features.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "conv_layers",
        "label": "Conv Layers",
        "type": "text",
        "default": "32,64,128",
        "tooltip": "Comma-separated output channels per convolutional block."
      },
      {
        "name": "kernel_size",
        "label": "Kernel Size",
        "type": "number",
        "default": 3,
        "range": "1 – 7",
        "tooltip": "Convolution kernel size (applied uniformly across blocks)."
      },
      {
        "name": "dropout",
        "label": "Dropout",
        "type": "number",
        "default": 0.25,
        "range": "0 – 0.5",
        "tooltip": "Dropout probability before the final classifier head."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.0005,
        "range": "0.00001 – 0.01",
        "tooltip": "Optimizer learning rate."
      },
      {
        "name": "batch_size",
        "label": "Batch Size",
        "type": "number",
        "default": 32,
        "range": "8 – 256",
        "tooltip": "Minibatch size — image batches are memory-heavier than tabular ones."
      },
      {
        "name": "epochs",
        "label": "Epochs",
        "type": "number",
        "default": 20,
        "range": "1 – 200",
        "tooltip": "Number of full passes over the training data."
      }
    ]
  },
  {
    "algorithmKey": "gru_classifier",
    "label": "GRU Classifier (PyTorch)",
    "appliesToText": "sequence classification — encodes a whole sequence (a session, a sensor window) into one label, distinct from gru-forecaster's next-value prediction.",
    "hasAppliesTo": false,
    "defaultAppliesTo": null,
    "appliesToTooltip": null,
    "appliesToOptions": null,
    "fields": [
      {
        "name": "hidden_size",
        "label": "Hidden Size",
        "type": "number",
        "default": 64,
        "range": "8 – 512",
        "tooltip": "GRU hidden state dimensionality."
      },
      {
        "name": "num_layers",
        "label": "Layers",
        "type": "number",
        "default": 1,
        "range": "1 – 4",
        "tooltip": "Number of stacked GRU layers."
      },
      {
        "name": "bidirectional",
        "label": "Bidirectional",
        "type": "select",
        "default": "false",
        "options": [
          "false",
          "true"
        ],
        "tooltip": "Encode the sequence in both directions — usually helps classification, unlike forecasting."
      },
      {
        "name": "learning_rate",
        "label": "Learning Rate",
        "type": "number",
        "default": 0.001,
        "range": "0.00001 – 0.1",
        "tooltip": "Optimizer learning rate."
      },
      {
        "name": "batch_size",
        "label": "Batch Size",
        "type": "number",
        "default": 64,
        "range": "8 – 512",
        "tooltip": "Minibatch size."
      },
      {
        "name": "epochs",
        "label": "Epochs",
        "type": "number",
        "default": 25,
        "range": "1 – 200",
        "tooltip": "Number of full passes over the training data."
      }
    ]
  }
];
