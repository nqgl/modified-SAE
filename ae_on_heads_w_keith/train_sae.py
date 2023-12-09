from . import z_sae
import wandb
import tqdm
import torch
from .calculations_on_sae import get_recons_loss, get_freqs, re_init
from transformer_lens import HookedTransformer
import time
# from . import buffer_dataset

def train(encoder :z_sae.AutoEncoder, cfg :z_sae.AutoEncoderConfig, buffer :z_sae.Buffer, model :HookedTransformer):
    wandb.login(key="0cb29a3826bf031cc561fd7447767a3d7920d888", relogin=True)
    t0 = time.time()
    # buffer.freshen_buffer(fresh_factor=0.5)

    try:
        run = wandb.init(project="autoencoders", entity="sae_all", config=cfg)
        # run = wandb.init(project="autoencoders", entity="sae_all", config=cfg, mode="disabled")

        num_batches = cfg.num_tokens // cfg.batch_size
        # model_num_batches = cfg.model_batch_size * num_batches
        # encoder_optim = torch.optim.Adam(encoder.parameters(), lr=cfg.lr, betas=(cfg.beta1, cfg.beta2))
        encoder_optim = torch.optim.AdamW(encoder.parameters(), lr=cfg.lr, betas=(cfg.beta1, cfg.beta2))
        recons_scores = []
        act_freq_scores_list = []
        for i in tqdm.trange(num_batches):
            # i = i % buffer.all_tokens.shape[0]
            acts = buffer.next()
            cache_embed_l0 = i % 100 == 0
            x_reconstruct = encoder(acts, record_activation_frequency=True, cache_embed_l0=record_embed_l0)
            # if i % 100 == 99:
            #     encoder.re_init_neurons_gram_shmidt(x.float() - x_reconstruct.float())
            loss = encoder.get_loss()
            l2_loss = encoder.l2_loss_cached.mean()
            l1_loss = encoder.l1_loss_cached.mean()
            l0_norm = encoder.l0_norm_cached.mean() # TODO condisder turning this off if is slows down calculation
            # scaler.scale(loss).backward()
            loss.backward()
            encoder.make_decoder_weights_and_grad_unit_norm()
            # scaler.step(encoder_optim)
            # scaler.update()
            encoder_optim.step()
            encoder_optim.zero_grad()
            if i % 200 == 99 and encoder.to_be_reset is not None:
                waiting = encoder.to_be_reset.shape[0]
                wandb.log({"neurons_waiting_to_reset": encoder.to_be_reset.shape[0]})
                encoder.re_init_neurons(acts.float() - x_reconstruct.float())
                if encoder.to_be_reset is not None:
                    num_reset = waiting - encoder.to_be_reset.shape[0]
                else:
                    num_reset = waiting
                wandb.log({"neurons_reset": num_reset})
                encoder_optim = torch.optim.AdamW(encoder.parameters(), lr=cfg.lr, betas=(cfg.beta1, cfg.beta2))
            loss_dict = {"loss": loss.item(), "l2_loss": l2_loss.item(), "l1_loss": l1_loss.sum().item(), "l0_norm": l0_norm.item()}
            del loss, x_reconstruct, l2_loss, l1_loss, acts, l0_norm
            if (i) % 100 == 0:
                loss_dict["embed_l0_norm"] = encoder.embed_l0_norm_cached.mean().item()
                wandb.log(loss_dict)
                print(loss_dict, run.name)
            if (i) % 5000 == 0:
                x = (get_recons_loss(model, encoder, buffer, local_encoder=encoder, num_batches=5))
                print("Reconstruction:", x)
                recons_scores.append(x[0])
                
                # freqs = get_freqs(model, encoder, buffer, 5, local_encoder=encoder)
                freqs = encoder.activation_frequency / encoder.steps_since_activation_frequency_reset
                act_freq_scores_list.append(freqs)
                # histogram(freqs.log10(), marginal="box",h istnorm="percent", title="Frequencies")
                wandb.log({
                    "recons_score": x[0],
                    "dead": (freqs==0).float().mean().item(),
                    "below_1e-6": (freqs<1e-6).float().mean().item(),
                    "below_1e-5": (freqs<1e-5).float().mean().item(),
                    "time spent shuffling": buffer.time_shuffling,
                    "total time" : time.time() - t0,
                })
            if i == 13501:
                encoder.reset_activation_frequencies()    
            elif i % 15000 == 13501 and i > 1500:
                encoder.save(name=run.name)
                t1 = time.time()
                # freqs = get_freqs(model, encoder, buffer, 50, local_encoder=encoder)
                freqs = encoder.activation_frequency / encoder.steps_since_activation_frequency_reset
                to_be_reset = (freqs<10**(-5.5))
                print("Resetting neurons!", to_be_reset.sum())
                if to_be_reset.sum() > 0:
                    encoder.neurons_to_reset(to_be_reset)
                    # re_init(model, encoder, buffer, to_be_reset)
                wandb.log({"reset_neurons": to_be_reset.sum(), "time_for_neuron_reset": time.time() - t1})
                encoder.reset_activation_frequencies()
    finally:
        encoder.save()

l1_coeff_list = [1e-3, 15e-4, 12e-4]

def linspace_l1(ae, l1_radius):
    cfg = ae.cfg
    l1 = torch.linspace(cfg.l1_coeff * (1 - l1_radius), cfg.l1_coeff * (1 + l1_radius), cfg.dict_size, device=cfg.device)
    ae.l1_coeff = l1

# conversions after fixing the sums over batch size
    # pre batch size reduction: multiply l1 by 256
    # post batch size reduction: divide l1 by 128
# this might not be the case either because l2 is now meaned too
# so like, l2 /= 512 
#          l1 /= 
# so maybe increase lr by 1-2.5 oom?
# l1 coeff prevv got multiplied by 128 - 256 but then l2 was like 256 times too
    # for l1 to get similar gradients, 
    
ae_cfg = z_sae.AutoEncoderConfig(site="z", act_size=512, layer=1, gram_shmidt_trail = 512, num_to_resample = 64,
                                l1_coeff=60e-4, dict_mult=32, batch_size=1024, beta2=0.99,
                                nonlinearity=("relu", {}), flatten_heads=True, buffer_mult=10000, buffer_refresh_ratio=0.4,
                                lr=3e-4, cosine_l1={"period": 620063, "range" : 0.0125}) #original 3e-4 8e-4 or same but 1e-3 on l1

def main():
    # ae_cfg = z_sae.AutoEncoderConfig(site="z", act_size=768, layer=1, model_name="gpt2-small",
    #                             l1_coeff=28e-4, dict_mult=8, batch_size=512, beta2=0.99,
    #                             nonlinearity=("relu", {}), flatten_heads=True, buffer_mult=400, buffer_r  efresh_ratio=0.5,
    #                             lr=3e-4, cosine_l1={"period": 62063, "range" : 0.05}) #original 3e-4 8e-4 or same but 1e-3 on l1

    # ae_cfg_z = z_sae.AutoEncoderConfig(site="z", act_size=512, 
    #                                  l1_coeff=2e-3,
    #                                  nonlinearity=("undying_relu", {"l" : 0.001, "k" : 0.1}), 
    #                                  lr=1e-4) #original 3e-4 8e-4 or same but 1e-3 on l1
    # cfg = z_sae.post_init_cfg(ae_cfg)
    model = z_sae.get_model(ae_cfg)
    all_tokens = z_sae.load_data(model)
    encoder = z_sae.AutoEncoder(cfg, model=model)
    # linspace_l1(encoder, 0.2)
    # dataloader, buffer = buffer_dataset.get_dataloader(cfg, all_tokens, model=model, device=torch.device("cpu"))
    # print(buffer.device)
    # buffer = buffer_dataset.BufferRefresher(cfg, all_tokens, model, device="cuda")
    buffer = z_sae.Buffer(cfg, all_tokens, model=model)
    train(encoder, cfg, buffer, model)

if __name__ == "__main__":
    main()