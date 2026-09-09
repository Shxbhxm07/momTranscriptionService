import os
import torch

def get_base_cfg():
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # os.getenv returns '' (empty string) when env var is set but blank,
    # and '' is not None so the default arg of os.getenv() is never used.
    # Use `or` to treat '' the same as unset.
    embedding_model = os.getenv('DIARIZATION_MODEL_PATH') or 'titanet_large'
    vad_model = os.getenv('VAD_MODEL_PATH') or 'vad_multilingual_marblenet'

    return {
        'device': device,
        'num_workers': 0,
        'sample_rate': 16000,
        'batch_size': 64,
        'verbose': True,
        'diarizer': {
            'manifest_filepath': None,
            'out_dir': '/tmp/nemo_outputs',
            'oracle_vad': False,
            'collar': 0.25,         # NeMo meeting default
            'ignore_overlap': True, # don't create a third phantom label for overlapping speech

            'speaker_embeddings': {
                'model_path': embedding_model,  # titanet_large — purpose-built diarization model
                'parameters': {
                    # 6-scale multiscale config from NeMo's official diar_infer_meeting.yaml.
                    # Longer windows (3.0s) capture full voice character; shorter (0.5s) catch
                    # fine-grained transitions. Combination is critical for 6-8 speakers where
                    # voices can be similar.
                    'window_length_in_sec': [3.0, 2.5, 2.0, 1.5, 1.0, 0.5],
                    'shift_length_in_sec':  [1.5, 1.25, 1.0, 0.75, 0.5, 0.25],
                    'multiscale_weights':   [1, 1, 1, 1, 1, 1],  # equal weight across all scales
                    'save_embeddings': True
                }
            },

            'clustering': {
                'parameters': {
                    'oracle_num_speakers': False,
                    'max_num_speakers': 8,
                    # NeMo default is 80. Enhanced speaker counting activates when
                    # num_segments < this threshold — it is significantly more accurate.
                    # Most business meetings have < 80 segments so this is almost always on.
                    # Raised from 80 → 150 so enhanced (more accurate) speaker counting
                    # stays active for typical 7-8 person meetings which generate 80-120 segments.
                    'enhanced_count_thres': 150,
                    # Raised from 0.30 → 0.45 to split similar-sounding voices more aggressively.
                    # At 0.30, a 7-person meeting collapsed to 4 clusters (speaker_2 contained 3 people).
                    # 0.45 is a safe upper bound: splits well without over-counting in 2-3 person meetings.
                    'max_rp_threshold': 0.45,
                    # Higher = more p-values sampled for majority vote = more stable count.
                    # 100 gives a much more reliable vote for 6–8 speaker meetings.
                    'sparse_search_volume': 100,
                    # Majority vote across p-values for more stable speaker count.
                    # Reduces sensitivity to a single bad p-value estimate.
                    'maj_vote_spk_count': True,
                }
            },

            'vad': {
                'model_path': vad_model,
                'parameters': {
                    'window_length_in_sec': 0.63,
                    'shift_length_in_sec': 0.02,
                    'smoothing': False,
                    'overlap': 0.5,
                    'onset': 0.4,               # higher = only clear speech, not breath/noise
                    'offset': 0.3,              # higher = don't end segment too eagerly
                    'pad_onset': 0.2,
                    'pad_offset': 0.1,
                    'min_duration_on': 0.2,     # 200ms min (NeMo docs default) — captures shorter turns
                    'min_duration_off': 0.20,   # 200ms silence to split (NeMo docs default).
                                                # 150ms was too aggressive — within-speaker pauses and
                                                # brief background noise (cough, chair) pass at 150ms,
                                                # adding noisy embeddings that confuse clustering.
                    'filter_speech_first': True
                }
            }
        }
    }
